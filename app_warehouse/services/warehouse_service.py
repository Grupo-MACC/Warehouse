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

#region process order
async def recibir_order_completa(
    db: AsyncSession,
    incoming_order: schemas.IncomingOrder,
) -> Tuple[models.WarehouseManufacturingOrder, List[dict]]:
    """Procesa una order completa que llega a Warehouse.

    Flujo:
    1) Idempotencia básica: si la order ya existe en fabricación, no hacemos nada.
    2) Calculamos totales A/B.
    3) Consumimos stock disponible (y lo descontamos).
    4) Creamos la order en fabricación.
    5) Insertamos en warehouse_order_piece las piezas cubiertas desde stock (source='stock').
    6) Devolvemos la lista de piezas a fabricar (para futuras colas A/B).

    Returns:
        (WarehouseManufacturingOrder, piezas_a_fabricar)
    """
    order_id = incoming_order.order_id

    existing = await crud.get_manufacturing_order(db, order_id)
    if existing is not None:
        logger.warning("[WAREHOUSE] Order %s ya existe. Idempotencia: se devuelve la existente.", order_id)
        return existing, []

    totals = _sumar_piezas_por_tipo(incoming_order)
    total_a, total_b = totals["A"], totals["B"]

    # 1) Consumir stock disponible para evitar fabricar piezas ya existentes
    used_a = await crud.consume_stock(db, "A", total_a)
    used_b = await crud.consume_stock(db, "B", total_b)

    to_build_a = total_a - used_a
    to_build_b = total_b - used_b

    # 2) La order puede quedar finished inmediatamente si todo se cubrió con stock
    finished = (to_build_a == 0 and to_build_b == 0)

    # 3) Crear order en fabricación
    db_order = await crud.create_manufacturing_order(
        db=db,
        order_id=order_id,
        total_a=total_a,
        total_b=total_b,
        to_build_a=to_build_a,
        to_build_b=to_build_b,
        finished=finished,
    )

    # 4) Registrar piezas aportadas desde stock como piezas "de la order"
    #    (una fila por pieza, igual que el microservicio Order hace con Piece)
    for _ in range(used_a):
        await crud.create_order_piece(db, order_id=order_id, piece_type="A", source="stock", manufacturing_date=None)

    for _ in range(used_b):
        await crud.create_order_piece(db, order_id=order_id, piece_type="B", source="stock", manufacturing_date=None)

    piezas_a_fabricar: List[dict] = []
    piezas_a_fabricar += _build_piece_messages(order_id=order_id, piece_type="A", qty=to_build_a)
    piezas_a_fabricar += _build_piece_messages(order_id=order_id, piece_type="B", qty=to_build_b)

    logger.info(
        "[WAREHOUSE] Order %s planned. total(A=%s,B=%s) used_stock(A=%s,B=%s) to_build(A=%s,B=%s) finished=%s",
        order_id, total_a, total_b, used_a, used_b, to_build_a, to_build_b, finished
    )

    return db_order, piezas_a_fabricar

#region piece manufactured
async def recibir_pieza_fabricada(
    db: AsyncSession,
    event: schemas.PieceBuiltEvent,
) -> models.WarehouseManufacturingOrder:
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
        raise ValueError(f"Order {event.order_id} no existe en fabricación.")

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

    # Recalcular finished comparando conteos con totales
    count_a = await crud.count_order_pieces_by_type(db, order_id=event.order_id, piece_type="A")
    count_b = await crud.count_order_pieces_by_type(db, order_id=event.order_id, piece_type="B")

    is_finished = (count_a >= db_order.total_a) and (count_b >= db_order.total_b)
    if is_finished and not db_order.finished:
        await crud.set_order_finished(db, order_id=event.order_id, finished=True)
        logger.info("[WAREHOUSE] Order %s FINISHED ✅ (A=%s/%s, B=%s/%s)",
                    event.order_id, count_a, db_order.total_a, count_b, db_order.total_b)

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
