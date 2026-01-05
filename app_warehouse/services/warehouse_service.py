# -*- coding: utf-8 -*-
"""Lógica interna (core) de Warehouse.

Este módulo contiene la lógica de negocio principal sin acoplarse a RabbitMQ:
- recibir_order_completa(): planifica la fabricación descontando stock disponible.
- recibir_pieza_fabricada(): registra piezas fabricadas y marca finished cuando toque.

Diseño deliberado:
- No hace publish real.
- Devuelve una "lista de piezas a fabricar" (una entrada por pieza) que en el futuro
  se publicará a colas A/B.
"""

import logging
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from sqlalchemy.ext.asyncio import AsyncSession

from sql import crud, models, schemas

logger = logging.getLogger(__name__)

#region 0. HELPER
def _sumar_piezas_por_tipo(order: schemas.IncomingOrder) -> Dict[str, int]:
    """Suma cantidades por tipo A/B a partir del payload de entrada.

    Returns:
        Dict[str, int]: {"A": total_a, "B": total_b}
    """
    totals = {"A": 0, "B": 0}

    for line in order.lines:
        # Pydantic ya valida PieceType, pero esto hace el código más defensivo
        if line.piece_type not in ("A", "B"):
            raise ValueError(f"Tipo de pieza inválido: {line.piece_type}")
        totals[line.piece_type] += line.quantity

    return totals


def _build_piece_messages(order_id: int, piece_type: str, qty: int) -> List[dict]:
    """Crea mensajes 'simulados' de piezas a fabricar (una entrada por pieza).

    En el futuro estos dicts se convertirán en mensajes RabbitMQ a:
    - cola A (si piece_type='A')
    - cola B (si piece_type='B')

    El campo 'date' es la fecha de publicación/planificación.
    """
    now = datetime.now(timezone.utc)
    return [
        {"order_id": order_id, "piece_type": piece_type, "date": now}
        for _ in range(max(qty, 0))
    ]

async def _recalculate_and_set_completed(db: AsyncSession, order_id: int) -> bool:
    """Recalcula si una order está completa comparando piezas registradas vs totales.

    Si completa:
        - status = COMPLETED
        - completed_at = now
    """
    db_order = await crud.get_manufacturing_order(db, order_id)
    if db_order is None:
        raise ValueError(f"Order {order_id} no existe en Warehouse.")

    if db_order.status in ("CANCELED",):
        return False

    count_a = await crud.count_order_pieces_by_type(db, order_id=order_id, piece_type="A")
    count_b = await crud.count_order_pieces_by_type(db, order_id=order_id, piece_type="B")

    is_completed = (count_a >= db_order.total_a) and (count_b >= db_order.total_b)

    if is_completed and db_order.status != "COMPLETED":
        db_order.status = "COMPLETED"
        db_order.completed_at = datetime.now(timezone.utc)
        await db.flush()
        logger.info("[WAREHOUSE] Order %s COMPLETED ✅", order_id)

    return is_completed


#region process order
async def recibir_order_completa(
    db: AsyncSession,
    incoming_order: schemas.IncomingOrder,
) -> Tuple[models.WarehouseOrder, List[dict]]:
    """Procesa una order completa que llega a Warehouse.

    Flujo:
    1) Idempotencia: si ya existe, no tocar stock ni re-planificar.
    2) Calcular totales A/B.
    3) Consumir stock disponible (descontándolo).
    4) Crear la order en BD.
    5) Registrar como piezas de la order las unidades cubiertas desde stock (source='stock').
    6) Devolver lista de piezas a fabricar (para publicar a máquinas).
    7) Recalcular COMPLETED basándose en piezas registradas vs totales.
    """
    order_id = incoming_order.order_id

    # 1) Idempotencia primero (CRÍTICO para no descontar stock dos veces si Rabbit reencola)
    existing = await crud.get_manufacturing_order(db, order_id)
    if existing is not None:
        logger.warning(
            "[WAREHOUSE] Order %s ya existe. Idempotencia: se devuelve la existente.",
            order_id,
        )
        return existing, []

    # 2) Totales
    totals = _sumar_piezas_por_tipo(incoming_order)
    total_a, total_b = totals["A"], totals["B"]

    # 3) Consumir stock
    used_a = await crud.consume_stock(db, "A", total_a)
    used_b = await crud.consume_stock(db, "B", total_b)

    to_build_a = total_a - used_a
    to_build_b = total_b - used_b

    # 4) Crear order (status por defecto IN_MANUFACTURING; ya la completará el recálculo si toca)
    db_order = await crud.create_manufacturing_order(
        db=db,
        order_id=order_id,
        total_a=total_a,
        total_b=total_b,
        to_build_a=to_build_a,
        to_build_b=to_build_b,
    )

    # 5) Registrar piezas cubiertas desde stock (una fila por pieza)
    for _ in range(used_a):
        await crud.create_order_piece(
            db=db,
            order_id=order_id,
            piece_type="A",
            source="stock",
            manufacturing_date=None,
        )

    for _ in range(used_b):
        await crud.create_order_piece(
            db=db,
            order_id=order_id,
            piece_type="B",
            source="stock",
            manufacturing_date=None,
        )

    # 6) Mensajes a fabricar (una entrada por pieza)
    piezas_a_fabricar: List[dict] = []
    piezas_a_fabricar += _build_piece_messages(order_id=order_id, piece_type="A", qty=to_build_a)
    piezas_a_fabricar += _build_piece_messages(order_id=order_id, piece_type="B", qty=to_build_b)

    # 7) Recalcular COMPLETED tras registrar piezas desde stock
    is_completed = await _recalculate_and_set_completed(db, order_id)

    logger.info(
        "[WAREHOUSE] Order %s planned. total(A=%s,B=%s) used_stock(A=%s,B=%s) "
        "to_build(A=%s,B=%s) status=%s completed=%s",
        order_id, total_a, total_b, used_a, used_b, to_build_a, to_build_b, db_order.status, is_completed,
    )

    return db_order, piezas_a_fabricar


#region piece manufactured
async def recibir_pieza_fabricada(
    db: AsyncSession,
    event: schemas.PieceBuiltEvent,
) -> models.WarehouseOrder:
    """Registra una pieza fabricada y recalcula si la order ya está completa.

    Reglas:
    - Si la order no existe: error.
    - Si ya hay suficientes piezas de ese tipo para la order: se ignora (idempotencia básica).
    - Si se completa el total de A y B: finished=True.

    Returns:
        WarehouseManufacturingOrder: la order actualizada (posiblemente finished).
    """
    db_order = await crud.get_manufacturing_order(db, event.order_id)
    if db_order is None:
        raise ValueError(f"Order {event.order_id} no existe en Warehouse.")

    if db_order.status == "CANCELED":
        # Piezas tardías: van a stock directamente
        await crud.add_stock(db, event.piece_type, 1)
        logger.info("[WAREHOUSE] Pieza tardía de order cancelada %s -> stock (%s)", event.order_id, event.piece_type)
        return db_order

    if event.piece_type not in ("A", "B"):
        raise ValueError(f"Tipo de pieza inválido: {event.piece_type}")

    # Total esperado para ese tipo según la order
    expected = db_order.total_a if event.piece_type == "A" else db_order.total_b

    # Cuántas hay ya registradas (stock + manufactured)
    current = await crud.count_order_pieces_by_type(db, order_id=event.order_id, piece_type=event.piece_type)

    # Si ya tenemos suficientes, ignoramos para evitar sobreconteo (y por reintentos futuros)
    if current >= expected:
        logger.warning(
            "[WAREHOUSE] Piece extra ignorada (idempotencia básica). order=%s type=%s current=%s expected=%s",
            event.order_id, event.piece_type, current, expected
        )
        # Aun así devolvemos la order (sin cambios)
        return db_order

    # Insertar la pieza fabricada
    manufacturing_date = event.manufacturing_date or datetime.now(timezone.utc)
    await crud.create_order_piece(
        db=db,
        order_id=event.order_id,
        piece_type=event.piece_type,
        source="manufactured",
        manufacturing_date=manufacturing_date,
    )
    await _recalculate_and_set_completed(db, event.order_id)

    return db_order

#region stock check
async def consultar_stock(
    db: AsyncSession,
    piece_type: str | None = None,
) -> list[models.WarehouseStock]:
    """Devuelve el stock disponible del almacén.

    Este endpoint es *de depuración* (ahora que no hay RabbitMQ real) para que puedas
    verificar rápidamente el estado del inventario.

    Reglas:
    - Si piece_type es None, asegura que existan filas para 'A' y 'B' y devuelve ambas.
    - Si piece_type es 'A' o 'B', asegura que exista esa fila y devuelve solo esa.

    Args:
        db: Sesión async de SQLAlchemy.
        piece_type: Tipo de pieza ('A' o 'B') o None para listar todo.

    Returns:
        list[WarehouseStock]: Filas ORM con {id, piece_type, quantity}.

    Raises:
        ValueError: Si piece_type no es 'A' ni 'B'.
    """
    if piece_type is None:
        row_a = await crud.get_or_create_stock_row(db, "A")
        row_b = await crud.get_or_create_stock_row(db, "B")
        return [row_a, row_b]

    if piece_type not in ("A", "B"):
        raise ValueError(f"piece_type inválido: {piece_type}. Usa 'A' o 'B'.")

    row = await crud.get_or_create_stock_row(db, piece_type)
    return [row]

async def anadir_stock(db: AsyncSession, piece_type: str, delta: int):
    """Servicio para sumar stock (debug).
    """
    return await crud.add_stock(db, piece_type, delta)


async def fijar_stock(db: AsyncSession, piece_type: str, quantity: int):
    """Servicio para fijar stock (debug).
    """
    return await crud.set_stock(db, piece_type, quantity)


#region order cancel
async def registrar_cancelacion(db, order_id: int, saga_id: str):
    """
    Registra cancelación como CANCEL_REQUESTED.

    Idempotencia:
        - Si ya existe, no duplica.
    """
    existing = await crud.get_cancel(db, order_id)
    if existing:
        return

    await crud.create_cancel(db, order_id, saga_id)


async def confirmar_cancelacion_maquina(db, order_id: int, machine: str):
    """
    Confirma cancelación recibida desde Machine.

    Según el SAGA, el evento evt.machine.canceled puede venir solo con order_id
    (sin identificar máquina). :contentReference[oaicite:1]{index=1}

    Compatibilidad:
        - machine == "A" / "B"
        - machine == "machine-a" / "machine-b" (y variantes)
        - machine None / "" / desconocido => confirmación global (marcamos ambas)
    """
    cancel = await crud.get_cancel(db, order_id)
    if not cancel:
        raise ValueError("Cancelación no registrada aún")

    m = (machine or "").strip().upper()

    def is_a(value: str) -> bool:
        """Detecta identificadores equivalentes a máquina A."""
        return value in {"A", "MACHINE-A", "MACHINE_A", "TYPE-A", "TYPE_A"}

    def is_b(value: str) -> bool:
        """Detecta identificadores equivalentes a máquina B."""
        return value in {"B", "MACHINE-B", "MACHINE_B", "TYPE-B", "TYPE_B"}

    if is_a(m):
        cancel.machine_a = True
    elif is_b(m):
        cancel.machine_b = True
    else:
        # Confirmación global: con blacklist compartida y sin broadcast,
        # cualquier réplica que confirme implica que todas dejarán de iniciar nuevas piezas.
        cancel.machine_a = True
        cancel.machine_b = True

    await db.flush()

    done = bool(cancel.machine_a and cancel.machine_b)
    return cancel.saga_id, done

async def aplicar_cancelacion_confirmada(db: AsyncSession, order_id: int) -> models.WarehouseOrder:
    """
    Aplica los efectos de dominio de una cancelación confirmada:

    1) Suma a stock TODAS las piezas registradas para esa order (stock + manufactured).
    2) Borra las piezas de la order (ya están en inventario).
    3) Marca la order como CANCELED (manteniendo el registro de la order).
    """
    db_order = await crud.get_manufacturing_order(db, order_id)
    if db_order is None:
        raise ValueError(f"Order {order_id} no existe en Warehouse.")

    # Idempotencia: si ya está cancelada, no repetir
    if db_order.status == "CANCELED":
        return db_order

    counts = await crud.get_piece_counts_for_order(db, order_id)
    if counts["A"] > 0:
        await crud.add_stock(db, "A", counts["A"])
    if counts["B"] > 0:
        await crud.add_stock(db, "B", counts["B"])

    await crud.delete_order_pieces(db, order_id)

    db_order.status = "CANCELED"
    db_order.canceled_at = datetime.now(timezone.utc)

    # (Opcional) ya no hay nada “por fabricar”
    db_order.to_build_a = 0
    db_order.to_build_b = 0

    await db.flush()

    logger.warning(
        "[WAREHOUSE] Cancelación aplicada: order=%s -> stock(A=%s,B=%s), piezas eliminadas, status=CANCELED",
        order_id, counts["A"], counts["B"]
    )

    return db_order

