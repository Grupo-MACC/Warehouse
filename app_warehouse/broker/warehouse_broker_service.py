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
#region HELPERS
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
#region CONSUMERS
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

#region piece
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
                    "[WAREHOUSE] ✅ Pieza registrada: order=%s type=%s finished=%s",
                    db_order.id, event.piece_type, db_order.finished
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

#region order canceled
async def consume_process_canceled_events():
    """Consume eventos process.canceled desde RabbitMQ. 
    Estos eventos indican que una order ha sido cancelada en el microservicio Order.

    Estrategia:
    - Escuchar en la cola process_canceled_queue
    - Procesar cada mensaje con handle_process_canceled
    """
    try:
        logger.info("[WAREHOUSE] 🔄 Iniciando consume_process_canceled_events...")
        print("[WAREHOUSE] 🔄 Iniciando consume_process_canceled_events...", flush=True)

        await publish_to_logger(message={"message": "Iniciando consume_process_canceled_events"}, topic="warehouse.info")

        # Obtenemos conexión y canal al broker
        _, channel = await get_channel()
        exchange = await declare_exchange(channel)

        queue = await channel.declare_queue("process_canceled_queue", durable=True)
        await queue.bind(exchange, routing_key="process.canceled")
        await queue.consume(handle_process_canceled)

        logger.info("[WAREHOUSE] 🟢 Escuchando eventos process.canceled...")
        print("[WAREHOUSE] 🟢 Escuchando eventos process.canceled...", flush=True)

        await publish_to_logger(message={"message": "Escuchando eventos process.canceled"}, topic="warehouse.info")

        # Mantener la corrutina viva
        await asyncio.Future()

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[WAREHOUSE] ❌ Error en consume_process_canceled_events: %s",
            exc,
            exc_info=True,
        )
        print(f"[WAREHOUSE] ❌ Error en consume_process_canceled_events: {exc}", flush=True)
        await publish_to_logger(
            message={"message": f"Error en consume_process_canceled_events: {exc}"},
            topic="warehouse.error",
        )


async def handle_process_canceled(message):
    """Procesa process.canceled.
    Estrategia:
    - Loguear el evento
    - (TODO) Añadir lógica para revertir stock, limpiar DB, etc.
    
    Args:
        message: Mensaje recibido de RabbitMQ.
    """

    async with message.process():
        try:
            data = json.loads(message.body)
            logger.warning("[WAREHOUSE] ⚠️ process.canceled recibido: %s", data)

            await publish_to_logger(
                message={"message": "process.canceled recibido", "payload": data},
                topic="warehouse.warn",
            )

            # LOG DE EVENTO (observability)
            await publish_to_logger(
                message={
                    "message": "Received domain event",
                    "event_type": "process.canceled",
                    "process_id": process_id,
                    "piece_type": piece_type,
                    "quantity": quantity,
                },
                topic="warehouse.info",
            )

            # LOG DEBUG opcional (payload crudo)
            await publish_to_logger(
                message={
                    "message": "Raw event payload",
                    "event_type": "process.canceled",
                    "process_id": process_id,
                    "payload": json.dumps(data),
                },
                topic="warehouse.debug",
            )

            # 🔧 Aquí, en iteraciones futuras:
            # - Llamar a un servicio/CRUD para registrar las piezas en almacén.
            #   Ejemplo:
            #   await warehouse_service.store_canceled_pieces(
            #       process_id=process_id,
            #       piece_type=piece_type,
            #       quantity=quantity,
            #   )

            # LOG FIN OK (opcional)
            await publish_to_logger(
                message={
                    "message": "Processed domain event",
                    "event_type": "process.canceled",
                    "process_id": process_id,
                    "result": "ok",
                },
                topic="warehouse.info",
            )

        except Exception as exc:  # noqa: BLE001
            logger.error("[WAREHOUSE] ❌ Error procesando process.canceled: %s", exc, exc_info=True)
            await publish_to_logger(
                message={
                    "message": "Error processing domain event",
                    "event_type": "process.canceled",
                    "process_id": process_id if "process_id" in locals() else None,
                    "error": str(exc),
                },
                topic="warehouse.error",
            )


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

#region logger
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

        # Asegúrate de que el mensaje tenga estos campos
        log_data = {
            "measurement": "logs",
            "service": topic.split('.')[0],
            "severity": topic.split('.')[1],
            **message
        }

        # Serializamos el mensaje a JSON
        body = json.dumps(log_data).encode()

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
