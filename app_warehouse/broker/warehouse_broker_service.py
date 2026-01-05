# -*- coding: utf-8 -*-
"""Servicios de integración de Warehouse con RabbitMQ.

Incluye:
- Consumer de process.canceled (ya existente).
- Consumer de orders entrantes para planificar fabricación.
- Publisher de piezas individuales hacia colas de máquinas A y B.

Notas importantes:
- Procesamos el mensaje y SOLO lo confirmamos (ack) cuando:
  1) hemos planificado (DB),
  2) hemos publicado piezas a las colas correspondientes,
  3) hemos hecho commit.
- Si algo falla, usamos requeue=True para no perder la order.
"""

import asyncio
import json
import logging
import os
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple
from sql import crud

from aio_pika import Message
from microservice_chassis_grupo2.core.rabbitmq_core import (
    get_channel,
    declare_exchange,
    declare_exchange_logs,
)

from sql.database import SessionLocal
from sql import schemas
from services import warehouse_service

logger = logging.getLogger(__name__)


# ----------------------------- Config Rabbit ---------------------------------

WAREHOUSE_ORDER_QUEUE = os.getenv("WAREHOUSE_ORDER_QUEUE", "warehouse_order_queue")
WAREHOUSE_ORDER_ROUTING_KEYS = [
    rk.strip()
    for rk in os.getenv("WAREHOUSE_ORDER_ROUTING_KEYS", "warehouse.order,order.created").split(",")
    if rk.strip()
]

MACHINE_A_ROUTING_KEY = os.getenv("MACHINE_A_ROUTING_KEY", "machine.a")
MACHINE_B_ROUTING_KEY = os.getenv("MACHINE_B_ROUTING_KEY", "machine.b")

WAREHOUSE_BUILT_QUEUE = os.getenv("WAREHOUSE_BUILT_QUEUE", "warehouse_built_queue")
WAREHOUSE_BUILT_ROUTING_KEYS = [
    rk.strip()
    for rk in os.getenv("WAREHOUSE_BUILT_ROUTING_KEYS", "piece.done").split(",")
    if rk.strip()
]


# ------------------------ Helpers de parsing payload --------------------------
#region 0. HELPERS
def _to_int(value: Any, default: int = 0) -> int:
    """Convierte valores a int de forma defensiva."""
    try:
        return int(value)
    except Exception:  # noqa: BLE001
        return default


def _extract_ab_counts(payload: Dict[str, Any]) -> Tuple[int, int]:
    """Extrae (A, B) de un payload aceptando variantes comunes.

    Acepta, por ejemplo:
        - {"pieces_a": 2, "pieces_b": 1}
        - {"num_a": 2, "num_b": 1}
        - {"a": 2, "b": 1}
        - {"A": 2, "B": 1}
        - {"lines":[{"piece_type":"A","quantity":2}, ...]}  -> en ese caso no se usa esto
    """
    a = (
        payload.get("pieces_a")
        or payload.get("num_a")
        or payload.get("a")
        or payload.get("A")
        or 0
    )
    b = (
        payload.get("pieces_b")
        or payload.get("num_b")
        or payload.get("b")
        or payload.get("B")
        or 0
    )
    return _to_int(a, 0), _to_int(b, 0)


def _payload_to_incoming_order(payload: Dict[str, Any]) -> schemas.IncomingOrder:
    """Convierte el JSON recibido por RabbitMQ a schemas.IncomingOrder.

    Formatos soportados:
      1) Formato “ideal” (ya compatible con tus endpoints):
         {"order_id": 1001, "lines":[{"piece_type":"A","quantity":2},{"piece_type":"B","quantity":1}]}

      2) Formato “compacto” (lo que describiste):
         {"order_id": 1001, "pieces_a":2, "pieces_b":1}
         {"order_id": 1001, "num_a":2, "num_b":1}
         {"order_id": 1001, "a":2, "b":1}

    Si llega algo raro, levantamos ValueError para poder requeue.
    """
    if "order_id" not in payload:
        raise ValueError("Payload inválido: falta order_id")

    order_id = _to_int(payload["order_id"], 0)
    if order_id <= 0:
        raise ValueError(f"order_id inválido: {payload.get('order_id')}")

    # Caso 1: lines
    if isinstance(payload.get("lines"), list) and payload["lines"]:
        return schemas.IncomingOrder(**payload)

    # Caso 2: compacto A/B
    a, b = _extract_ab_counts(payload)
    lines: List[Dict[str, Any]] = []
    if a > 0:
        lines.append({"piece_type": "A", "quantity": a})
    if b > 0:
        lines.append({"piece_type": "B", "quantity": b})

    if not lines:
        raise ValueError("Payload inválido: la order no incluye piezas A ni B (A=0, B=0)")

    return schemas.IncomingOrder(order_id=order_id, lines=lines)

def _extract_order_date_iso(payload: Dict[str, Any]) -> str:
    """Devuelve la fecha ISO de la order.

    Preferencias:
    - payload["order_date"]
    - payload["date"]
    - now() UTC
    """
    order_date = payload.get("order_date") or payload.get("date")
    if isinstance(order_date, str) and order_date.strip():
        return order_date.strip()
    return datetime.now(timezone.utc).isoformat()


def _payload_to_piece_built_event(payload: Dict[str, Any]) -> schemas.PieceBuiltEvent:
    """Convierte el JSON recibido por RabbitMQ a schemas.PieceBuiltEvent.

    Formatos aceptados:
      - {"order_id": 1001, "piece_type": "A", "manufacturing_date": "..."}
      - {"order_id": 1001, "type": "A", "date": "..."}  (compat)
    """
    if "order_id" not in payload:
        raise ValueError("Payload inválido: falta order_id")

    if "piece_type" not in payload and "type" in payload:
        payload["piece_type"] = payload["type"]

    if "manufacturing_date" not in payload:
        # compat con otros nombres
        if "date" in payload:
            payload["manufacturing_date"] = payload["date"]
        elif "manufactured_at" in payload:
            payload["manufacturing_date"] = payload["manufactured_at"]

    return schemas.PieceBuiltEvent(**payload)


# ------------------------------ Consumers -------------------------------------
#region 1. CONSUMERS
async def consume_incoming_orders():
    """Consume orders entrantes y dispara publicación de fabricación a máquinas A/B."""
    logger.info("[WAREHOUSE] 🔄 Iniciando consume_incoming_orders...")
    _, channel = await get_channel()
    exchange = await declare_exchange(channel)

    queue = await channel.declare_queue(WAREHOUSE_ORDER_QUEUE, durable=True)
    for rk in WAREHOUSE_ORDER_ROUTING_KEYS:
        await queue.bind(exchange, routing_key=rk)

    await queue.consume(handle_incoming_order)

    logger.info(
        "[WAREHOUSE] 🟢 Escuchando orders en '%s' (routing_keys=%s)",
        WAREHOUSE_ORDER_QUEUE,
        WAREHOUSE_ORDER_ROUTING_KEYS,
    )
    await publish_to_logger(
        message={
            "message": "🟢 Escuchando orders entrantes",
            "queue": WAREHOUSE_ORDER_QUEUE,
            "routing_keys": WAREHOUSE_ORDER_ROUTING_KEYS,
        },
        topic="warehouse.info",
    )

    await asyncio.Future()

async def handle_incoming_order(message):
    """Handler principal: recibe order, planifica y publica piezas a máquinas.

    Estrategia:
    - Parse JSON -> IncomingOrder
    - DB: recibir_order_completa() (sin commit aún)
    - Publicar piezas a máquinas (A/B)
    - Commit DB
    - Ack automático al salir del context manager
    """
    async with message.process(requeue=True):
        payload = json.loads(message.body)
        order_date_iso = _extract_order_date_iso(payload)
        incoming_order = _payload_to_incoming_order(payload)

        # 1) DB: planificar (sin commit todavía)
        async with SessionLocal() as db:
            try:
                db_order, piezas_a_fabricar = await warehouse_service.recibir_order_completa(db, incoming_order)

                # 2) Publicar fabricación (si hay algo que fabricar)
                await publish_pieces_to_machines(
                    piezas_a_fabricar=piezas_a_fabricar,
                    order_date_iso=order_date_iso,
                )

                # 3) Commit SOLO si publicar ha ido bien
                await db.commit()

            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                logger.error("[WAREHOUSE] ❌ Error procesando order entrante: %s", exc, exc_info=True)
                await publish_to_logger(
                    message={"message": "Error procesando order entrante", "error": str(exc), "payload": payload},
                    topic="warehouse.error",
                )
                raise

#region 1.1 piece
async def consume_built_pieces():
    """Consume eventos de piezas fabricadas desde RabbitMQ y las registra en BD.
    
     Estrategia:
     - Escuchar en la cola warehouse_built_queue
     - Procesar cada mensaje con handle_built_piece
    """
    logger.info("[WAREHOUSE] 🔄 Iniciando consume_built_pieces...")
    _, channel = await get_channel()
    exchange = await declare_exchange(channel)

    queue = await channel.declare_queue(WAREHOUSE_BUILT_QUEUE, durable=True)
    for rk in WAREHOUSE_BUILT_ROUTING_KEYS:
        await queue.bind(exchange, routing_key=rk)

    await queue.consume(handle_built_piece)

    logger.info(
        "[WAREHOUSE] 🟢 Escuchando piezas fabricadas en '%s' (routing_keys=%s)",
        WAREHOUSE_BUILT_QUEUE,
        WAREHOUSE_BUILT_ROUTING_KEYS,
    )

    await asyncio.Future()


async def handle_built_piece(message):
    """Procesa una pieza fabricada: inserta WarehouseOrderPiece y recalcula finished.
    Estrategia:
    - Parse JSON -> PieceBuiltEvent
    - DB: recibir_pieza_fabricada() (con commit)
    - Ack automático al salir del context manager
    
    Args:
        message: Mensaje recibido de RabbitMQ.
    """
    async with message.process(requeue=True):
        payload = json.loads(message.body)

        # 1) Validación / mapping del payload
        try:
            event = _payload_to_piece_built_event(payload)
        except Exception as exc:  # payload corrupto: NO reintentes infinito
            logger.error("[WAREHOUSE] ❌ Payload inválido en built_piece: %s | payload=%s", exc, payload)
            await publish_to_logger(
                message={"message": "Payload inválido en built_piece", "error": str(exc), "payload": payload},
                topic="warehouse.error",
            )
            return  # se ACKea y se descarta

        # 2) BD: registrar pieza + commit
        async with SessionLocal() as db:
            try:
                db_order = await warehouse_service.recibir_pieza_fabricada(db, event)
                await db.commit()

                logger.info(
                    "[WAREHOUSE] ✅ Pieza registrada: order=%s type=%s status=%s",
                    db_order.id, event.piece_type, db_order.status
                )

            except ValueError as exc:
                # Si la order no existe (race: pieza llega antes que la order), requeue
                msg = str(exc)
                logger.warning("[WAREHOUSE] ⚠️ No se pudo registrar pieza: %s | payload=%s", msg, payload)

                await db.rollback()

                if "no existe" in msg.lower():
                    raise  # requeue=True -> se reencola

                # otros ValueError: ACK y fuera, para evitar poison loop
                await publish_to_logger(
                    message={"message": "Error registrando pieza", "error": msg, "payload": payload},
                    topic="warehouse.warn",
                )
                return

            except Exception as exc:
                await db.rollback()
                logger.error("[WAREHOUSE] ❌ Error inesperado registrando pieza: %s", exc, exc_info=True)
                await publish_to_logger(
                    message={"message": "Error inesperado registrando pieza", "error": str(exc), "payload": payload},
                    topic="warehouse.error",
                )
                raise

#region 2. ORDER CANCEL
RK_CMD_CANCEL_MFG = "cmd.cancel_manufacturing"
RK_EVT_MFG_CANCELED = "evt.manufacturing_canceled"

RK_CMD_MACHINE_CANCEL = "cmd.machine.cancel"
RK_EVT_MACHINE_CANCELED = "evt.machine.canceled"

WAREHOUSE_CANCEL_QUEUE = "warehouse_cancel_queue"
WAREHOUSE_MACHINE_CANCELED_QUEUE = "warehouse_machine_canceled_queue"

async def consume_process_canceled_events():
    """
    Consume el comando cmd.cancel_manufacturing enviado por Order.

    Flujo:
        1) Validar payload (order_id, saga_id)
        2) Persistir cancelación (estado CANCEL_REQUESTED)
        3) Publicar cmd.machine.cancel a máquinas
        4) Esperar confirmaciones (evt.machine.canceled)
    """
    try:
        logger.info("[WAREHOUSE] 🔄 Escuchando %s...", RK_CMD_CANCEL_MFG)

        _, channel = await get_channel()
        exchange = await declare_exchange(channel)

        queue = await channel.declare_queue(WAREHOUSE_CANCEL_QUEUE, durable=True)
        await queue.bind(exchange, routing_key=RK_CMD_CANCEL_MFG)

        await queue.consume(handle_process_canceled)
        await asyncio.Future()

    except Exception as exc:  # noqa: BLE001
        logger.error("[WAREHOUSE] ❌ Error en consume_process_canceled_events: %s", exc, exc_info=True)

async def handle_process_canceled(message):
    """Handler del comando cmd.cancel_manufacturing (Order -> Warehouse).

    Payload esperado:
        {
            "order_id": int,
            "saga_id": str
        }

    Comportamiento:
        1) Validar payload (sin tragarte errores internos).
        2) En BD:
            - comprobar que la order existe
            - poner status=CANCELING
            - guardar cancel_saga_id
            - registrar fila de cancelación (idempotente)
        3) Publicar cmd.machine.cancel (hacia máquinas)
        4) Commit
    """
    async with message.process(requeue=True):
        # 1) Validación estricta del payload (NO uses "except Exception" aquí)
        try:
            payload = json.loads(message.body)
            order_id = int(payload["order_id"])
            saga_id = str(payload["saga_id"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.error("[WAREHOUSE] ❌ Payload inválido en cmd.cancel_manufacturing: %s | payload=%s", exc, payload if "payload" in locals() else None)
            return  # ACK y descartado (poison message)

        # 2) BD: aplicar CANCELING + registrar cancelación
        async with SessionLocal() as db:
            try:
                order = await crud.get_manufacturing_order(db, order_id)
                if order is None:
                    # Si Order te manda cancel de algo que Warehouse no conoce, mejor log y ACK
                    logger.warning("[WAREHOUSE] ⚠️ Cancelación recibida para order inexistente: %s (saga=%s)", order_id, saga_id)
                    return

                # Idempotencia / estados terminales
                if order.status in ("CANCELED", "COMPLETED"):
                    logger.info("[WAREHOUSE] ℹ️ Cancel ignorado: order=%s ya está en estado terminal=%s", order_id, order.status)
                    return

                # Marcar cancelación en la order (fuente de verdad)
                order.status = "CANCELING"
                order.cancel_saga_id = saga_id
                await db.flush()

                # Tabla auxiliar de cancelación (idempotente)
                await warehouse_service.registrar_cancelacion(db, order_id, saga_id)

                # 3) Publicar cancel a máquinas
                await publish_machine_cancel(order_id)

                # 4) Commit
                await db.commit()
                logger.info("[WAREHOUSE] ✅ Cancelación iniciada: order=%s saga=%s -> cmd.machine.cancel publicado", order_id, saga_id)

            except Exception as exc:
                await db.rollback()
                logger.error("[WAREHOUSE] ❌ Error procesando cmd.cancel_manufacturing: %s | payload=%s", exc, payload, exc_info=True)
                raise  # requeue=True -> Rabbit reintenta


#region 2.1 cancel machine
async def consume_machine_canceled_events():
    """
    Consume evt.machine.canceled emitido por máquinas cuando ya han aplicado cancelación.

    Cuando Warehouse recibe confirmación suficiente (A y B), publica:
        evt.manufacturing_canceled(order_id, saga_id)
    """
    logger.info("[WAREHOUSE] 🔄 Escuchando %s...", RK_EVT_MACHINE_CANCELED)

    _, channel = await get_channel()
    exchange = await declare_exchange(channel)

    queue = await channel.declare_queue(WAREHOUSE_MACHINE_CANCELED_QUEUE, durable=True)
    await queue.bind(exchange, routing_key=RK_EVT_MACHINE_CANCELED)

    await queue.consume(handle_machine_canceled)
    await asyncio.Future()


async def handle_machine_canceled(message):
    """
    Handler de evt.machine.canceled.

    Payload esperado:
        {
            "order_id": int,
            "machine": "A"|"B"
        }

    Acciones:
        - Marcar máquina como confirmada
        - Si A o B confirmaron, emitir evt.manufacturing_canceled hacia Order
    """
    async with message.process(requeue=True):
        payload = json.loads(message.body)

        order_id = int(payload["order_id"])
        machine = payload.get("machine")

        try:
            async with SessionLocal() as db:
                saga_id, done = await warehouse_service.confirmar_cancelacion_maquina(db, order_id, machine)
                await db.commit()
        except ValueError as exc:
            logger.warning("[WAREHOUSE] ⚠️ evt.machine.canceled ignorado (%s). payload=%s", exc, payload)
            return
        except Exception as exc:
            logger.error("[WAREHOUSE] ❌ Error procesando evt.machine.canceled. payload=%s", payload, exc_info=True)
            raise

        if done:
            async with SessionLocal() as db:
                order = await warehouse_service.aplicar_cancelacion_confirmada(db, order_id)
                saga_id = order.cancel_saga_id  # guardado cuando llegó cmd.cancel_manufacturing
                await db.commit()
            await publish_manufacturing_canceled(order_id, saga_id)

async def publish_machine_cancel(order_id: int):
    """
    Publica cmd.machine.cancel para que las máquinas detengan fabricación.

    Payload:
        {
            "order_id": int
        }
    """
    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange(channel)

        payload = {"order_id": order_id}
        msg = Message(
            body=json.dumps(payload).encode(),
            content_type="application/json",
            delivery_mode=2,
        )
        await exchange.publish(msg, routing_key=RK_CMD_MACHINE_CANCEL)
        logger.info("[WAREHOUSE] 📤 cmd.machine.cancel → %s", payload)

    finally:
        await connection.close()

#region 2.2 cancel finished
async def publish_manufacturing_canceled(order_id: int, saga_id: str):
    """
    Publica evt.manufacturing_canceled hacia Order.

    Payload:
        {
            "order_id": int,
            "saga_id": str
        }
    """
    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange(channel)

        payload = {"order_id": order_id, "saga_id": saga_id}
        msg = Message(
            body=json.dumps(payload).encode(),
            content_type="application/json",
            delivery_mode=2,
        )
        await exchange.publish(msg, routing_key=RK_EVT_MFG_CANCELED)
        logger.info("[WAREHOUSE] 📤 evt.manufacturing_canceled → %s", payload)

    finally:
        await connection.close()


# ------------------------------ Publishers ------------------------------------
#region PUBLISHERS
async def publish_pieces_to_machines(piezas_a_fabricar: List[dict], order_date_iso: str):
    """Publica piezas individuales a colas de máquinas A/B.

    Cada mensaje tendrá:
        - piece_id (uuid)
        - order_id
        - piece_type
        - order_date (ISO string)
    """
    if not piezas_a_fabricar:
        logger.info("[WAREHOUSE] ✅ Order cubierta por stock: no hay nada que publicar a máquinas.")
        return

    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange(channel)

        for pieza in piezas_a_fabricar:
            piece_type = pieza.get("piece_type")
            order_id = pieza.get("order_id")

            if piece_type not in ("A", "B"):
                raise ValueError(f"piece_type inválido en piezas_a_fabricar: {piece_type}")

            routing_key = MACHINE_A_ROUTING_KEY if piece_type == "A" else MACHINE_B_ROUTING_KEY

            msg_payload = {
                "piece_id": str(uuid.uuid4()),
                "order_id": order_id,
                "piece_type": piece_type,
                "order_date": order_date_iso,
            }

            body = json.dumps(msg_payload).encode("utf-8")
            msg = Message(
                body=body,
                content_type="application/json",
                delivery_mode=2,  # persistente
            )
            await exchange.publish(message=msg, routing_key=routing_key)

        logger.info(
            "[WAREHOUSE] 📤 Publicadas %s piezas a máquinas (A->%s, B->%s)",
            len(piezas_a_fabricar),
            MACHINE_A_ROUTING_KEY,
            MACHINE_B_ROUTING_KEY,
        )
        await publish_to_logger(
            message={
                "message": "📤 Publicadas piezas a máquinas",
                "count": len(piezas_a_fabricar),
                "machine_a_routing_key": MACHINE_A_ROUTING_KEY,
                "machine_b_routing_key": MACHINE_B_ROUTING_KEY,
            },
            topic="warehouse.info",
        )

    finally:
        await connection.close()

#region LOGGER
async def publish_to_logger(message: dict, topic: str):
    """Publica logs en el exchange de logs.

    topic ejemplo:
        - "warehouse.info"
        - "warehouse.error"
    """
    connection = None
    try:
        connection, channel = await get_channel()
        exchange = await declare_exchange_logs(channel)

        log_data = {
            "measurement": "logs",
            "service": topic.split(".")[0],
            "severity": topic.split(".")[1] if "." in topic else "info",
            **message,
        }

        msg = Message(
            body=json.dumps(log_data).encode("utf-8"),
            content_type="application/json",
            delivery_mode=2,  # ✅ FIX: antes tenías warehouse_mode
        )

        await exchange.publish(message=msg, routing_key=topic)

    except Exception as exc:  # noqa: BLE001
        logger.error("[WAREHOUSE] ❌ Error publicando en logger: %s", exc, exc_info=True)
    finally:
        if connection:
            await connection.close()
