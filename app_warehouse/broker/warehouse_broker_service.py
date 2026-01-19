# -*- coding: utf-8 -*-
"""Broker RabbitMQ del microservicio Warehouse.

Este módulo integra Warehouse con RabbitMQ para:
    1) Recibir órdenes entrantes y planificar fabricación (DB) + publicar piezas a máquinas.
    2) Recibir eventos de piezas fabricadas (piece.done) y registrar stock/piezas (DB).
    3) Gestionar la SAGA de cancelación en fabricación (Order -> Warehouse -> Machine -> Warehouse -> Order).
    4) Publicar logs estructurados al exchange de logs.

Decisiones importantes:
    - ACK de mensajes SOLO cuando la operación completa ha ido bien.
    - Para errores internos: requeue=True (se reintenta).
    - Para payload inválido/poison: ACK y descartar (sin requeue) para evitar bucles infinitos.
    - NO dependemos de .env: docker-compose no define env vars específicas para colas/routing keys,
      por lo que se definen como constantes globales.
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
import os
import httpx
from datetime import datetime, timezone
from typing import Any, Dict, List, Tuple

from aio_pika import Message

from microservice_chassis_grupo2.core.rabbitmq_core import (
    PUBLIC_KEY_PATH,
    declare_exchange,
    declare_exchange_logs,
    declare_exchange_saga_cancelation_commands,
    declare_exchange_saga_cancelation_events,
    get_channel,
)

from sql.database import SessionLocal
from sql import crud, schemas
from services import warehouse_service
from microservice_chassis_grupo2.core.consul import get_service_url
logger = logging.getLogger(__name__)

# =============================================================================
# Constantes RabbitMQ (routing keys / colas / topics)
# =============================================================================

# ------------------ Routing keys: Auth (estado del servicio) -----------------
RK_AUTH_RUNNING = "auth.running"
RK_AUTH_NOT_RUNNING = "auth.not_running"
Q_AUTH_EVENTS = "order_queue"

# ------------------ Órdenes entrantes (Order -> Warehouse) --------------------
QUEUE_INCOMING_ORDERS = "warehouse_order_queue"
RK_INCOMING_ORDERS = "order.confirmed"

# ------------------ Publicación a máquinas (Warehouse -> Machine) -------------
RK_MACHINE_A = "todo.machine.a"
RK_MACHINE_B = "todo.machine.b"

# ------------------ Piezas fabricadas (Machine -> Warehouse) -----------------
QUEUE_BUILT_PIECES = "warehouse_built_queue"
RK_BUILT_PIECES = ("piece.done",)

# ------------------ Órdenes finalizadas (Warehouse -> Order) -----------------
RK_EVT_FABRICATION_COMPLETED = "warehouse.fabrication.completed"

# ------------------ SAGA cancelación fabricación -----------------------------
# Comando entrante desde Order
RK_CMD_CANCEL_FABRICATION = "cmd.cancel_fabrication"       # legacy/compat
QUEUE_CANCEL_FABRICATION = "warehouse_cancel_queue"

# Comando saliente hacia máquinas
RK_CMD_MACHINE_CANCEL = "cmd.machine.cancel"

# Evento entrante desde máquinas
RK_EVT_MACHINE_CANCELED = "evt.machine.canceled"
QUEUE_MACHINE_CANCELED = "warehouse_machine_canceled_queue"

# Evento saliente hacia Order
RK_EVT_FABRICATION_CANCELED = "evt.fabrication_canceled"      # legacy/compat

# ------------------ Logger topics --------------------------------------------
TOPIC_INFO = "warehouse.info"
TOPIC_WARN = "warehouse.warn"
TOPIC_ERROR = "warehouse.error"


# =============================================================================
# 1) HELPERS GENÉRICOS (publicación / parsing / validación)
# =============================================================================
#region 0. HELPERS
def _build_json_message(payload: dict) -> Message:
    """Construye un Message JSON persistente (delivery_mode=2).

    Args:
        payload: dict serializable a JSON.

    Returns:
        aio_pika.Message listo para publish().
    """
    return Message(
        body=json.dumps(payload).encode("utf-8"),
        content_type="application/json",
        delivery_mode=2,
    )


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

    Returns:
        (a, b) como ints >= 0
    """
    a = payload.get("pieces_a") or payload.get("num_a") or payload.get("a") or payload.get("A") or 0
    b = payload.get("pieces_b") or payload.get("num_b") or payload.get("b") or payload.get("B") or 0
    return _to_int(a, 0), _to_int(b, 0)


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


def _payload_to_incoming_order(payload: Dict[str, Any]) -> schemas.IncomingOrder:
    """Convierte el JSON recibido por RabbitMQ a schemas.IncomingOrder.

    Formatos soportados:
        1) Formato con lines:
           {"order_id": 1001, "lines":[{"piece_type":"A","quantity":2}, ...]}

        2) Formato compacto:
           {"order_id": 1001, "pieces_a":2, "pieces_b":1}
           {"order_id": 1001, "num_a":2, "num_b":1}
           {"order_id": 1001, "a":2, "b":1}

    Raises:
        ValueError: si el payload no es válido (para descartar sin requeue).
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


def _payload_to_piece_built_event(payload: Dict[str, Any]) -> schemas.PieceBuiltEvent:
    """Convierte JSON recibido a schemas.PieceBuiltEvent (compat).

    Acepta:
        - {"order_id": 1001, "piece_type": "A", "fabrication_date": "..."}
        - {"order_id": 1001, "type": "A", "date": "..."}  (compat)
        - {"fabricated_at": "..."} (compat)
    """
    if "order_id" not in payload:
        raise ValueError("Payload inválido: falta order_id")

    if "piece_type" not in payload and "type" in payload:
        payload["piece_type"] = payload["type"]

    if "fabrication_date" not in payload:
        if "date" in payload:
            payload["fabrication_date"] = payload["date"]
        elif "fabricated_at" in payload:
            payload["fabrication_date"] = payload["fabricated_at"]

    return schemas.PieceBuiltEvent(**payload)


def _internal_ca_file() -> str:
    """
    Devuelve la ruta del CA bundle para llamadas internas HTTPS.

    Por qué:
        - Los microservicios están usando certificados firmados por una CA privada.
        - httpx por defecto valida contra el bundle del sistema/certifi.
        - Si no le pasas tu CA, obtendrás CERTIFICATE_VERIFY_FAILED.

    Prioridad:
        1) INTERNAL_CA_FILE
        2) CONSUL_CA_FILE
        3) /certs/ca.pem (convención del proyecto)
    """
    return os.getenv("INTERNAL_CA_FILE") or os.getenv("CONSUL_CA_FILE") or "/certs/ca.pem"

async def _download_auth_public_key(auth_base_url: str) -> str:
    """
    Descarga la clave pública de Auth usando HTTPS con verificación por CA privada.

    Args:
        auth_base_url: Base URL (p.ej. "https://auth:5004")

    Returns:
        El texto PEM de la clave pública.

    Nota:
        - Separar esta función facilita reintentos.
    """
    async with httpx.AsyncClient(verify=_internal_ca_file(), timeout=5.0) as client:
        resp = await client.get(f"{auth_base_url}/auth/public-key")
        resp.raise_for_status()
        return resp.text


async def _ensure_auth_public_key(max_attempts: int = 20, base_delay: float = 0.25) -> None:
    """
    Asegura que existe la clave pública de Auth en PUBLIC_KEY_PATH.

    Estrategia simple:
        - Intenta resolver Auth por Consul (passing=true).
        - Si aún no hay instancias passing (race al arrancar), reintenta con backoff.
        - Cuando lo resuelve, descarga la clave con TLS verify (CA privada) y la guarda.

    Por qué:
        - auth.running se publica antes de que Auth esté realmente "ready" (FastAPI aún no sirve HTTP).
        - Por tanto, al recibir el evento, Consul puede devolver 0 passing temporalmente.
    """
    for attempt in range(1, max_attempts + 1):
        try:
            auth_base_url = await get_service_url("auth")
            public_key = await _download_auth_public_key(auth_base_url)

            # Escritura directa (simple). Si quieres más robustez: escribir a .tmp y renombrar.
            with open(PUBLIC_KEY_PATH, "w", encoding="utf-8") as f:
                f.write(public_key)

            logger.info("[WAREHOUSE] ✅ Clave pública de Auth guardada en %s", PUBLIC_KEY_PATH)
            return

        except Exception as exc:
            # OJO: esto NO es un error grave. Es normal durante el arranque.
            logger.warning(
                "[WAREHOUSE] ⏳ Auth aún no está 'passing' o no responde. Reintento %s/%s. Motivo: %s",
                attempt, max_attempts, exc
            )

            # Backoff suave (capado)
            delay = min(2.0, base_delay * (2 ** (attempt - 1)))
            await asyncio.sleep(delay)

    raise RuntimeError("No se pudo obtener la clave pública de Auth tras varios reintentos.")


# =============================================================================
# 2) CONSUMERS: órdenes entrantes + piezas fabricadas
# =============================================================================
#region 2. CONSUMERS
async def consume_incoming_orders() -> None:
    """Consume órdenes entrantes y dispara publicación de fabricación a máquinas A/B."""
    logger.info("[WAREHOUSE] 🔄 Iniciando consume_incoming_orders...")
    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange(channel)

        queue = await channel.declare_queue(QUEUE_INCOMING_ORDERS, durable=True)
        await queue.bind(exchange, routing_key=RK_INCOMING_ORDERS)

        await queue.consume(handle_incoming_order)

        logger.info("[WAREHOUSE] 🟢 Escuchando orders en '%s' (routing_keys=%s)", QUEUE_INCOMING_ORDERS, RK_INCOMING_ORDERS)
        await publish_to_logger(
            {"message": "Escuchando orders entrantes", "queue": QUEUE_INCOMING_ORDERS, "routing_keys": RK_INCOMING_ORDERS},
            TOPIC_INFO,
        )

        await asyncio.Future()
    finally:
        await connection.close()


async def handle_incoming_order(message) -> None:
    """Handler: recibe order, planifica y publica piezas a máquinas.

    Estrategia:
        - Parse JSON -> IncomingOrder
        - DB: recibir_order_completa() (sin commit aún)
        - Publicar piezas a máquinas (A/B)
        - Commit DB
        - ACK automático al salir del context manager

    Importante:
        - Payload inválido NO debe requeue (poison loop).
        - Errores internos sí requeue (requeue=True).
    """
    async with message.process(requeue=True):
        payload = json.loads(message.body)

        # 1) Parse + validación (si falla -> ACK y descartar)
        try:
            order_date_iso = _extract_order_date_iso(payload)
            incoming_order = _payload_to_incoming_order(payload)
        except Exception as exc:  # payload corrupto o inválido
            logger.error("[WAREHOUSE] ❌ Payload inválido en incoming_order: %s | payload=%s", exc, payload)
            await publish_to_logger(
                {"message": "Payload inválido en incoming_order", "error": str(exc), "payload": payload},
                TOPIC_ERROR,
            )
            return  # ACK y descartado

        # 2) BD: planificar (sin commit todavía)
        async with SessionLocal() as db:
            try:
                db_order, piezas_a_fabricar = await warehouse_service.recibir_order_completa(db, incoming_order)

                # 3) Publicar fabricación (si hay algo que fabricar)
                await publish_pieces_to_machines(
                    piezas_a_fabricar=piezas_a_fabricar,
                    order_date_iso=order_date_iso,
                )

                # 4) Commit SOLO si publish ha ido bien
                await db.commit()

                logger.info("[WAREHOUSE] ℹ️  Order planificada: order_id=%s status=%s", db_order.id, db_order.status)
                await publish_to_logger(
                    {"message": "Order planificada", "order_id": int(db_order.id), "status": str(db_order.status)},
                    TOPIC_INFO,
                )

            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                logger.error("[WAREHOUSE] ❌ Error procesando order entrante: %s", exc, exc_info=True)
                await publish_to_logger(
                    {"message": "Error procesando order entrante", "error": str(exc), "payload": payload},
                    TOPIC_ERROR,
                )
                raise  # requeue=True -> se reintenta

#region 1.1 piece
async def consume_built_pieces() -> None:
    """Consume eventos de piezas fabricadas desde RabbitMQ y las registra en BD."""
    logger.info("[WAREHOUSE] 🔄 Iniciando consume_built_pieces...")
    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange(channel)

        queue = await channel.declare_queue(QUEUE_BUILT_PIECES, durable=True)
        for rk in RK_BUILT_PIECES:
            await queue.bind(exchange, routing_key=rk)

        await queue.consume(handle_built_piece)

        logger.info("[WAREHOUSE] 🟢 Escuchando piezas fabricadas en '%s' (routing_keys=%s)", QUEUE_BUILT_PIECES, RK_BUILT_PIECES)
        await publish_to_logger(
            {"message": "Escuchando piezas fabricadas", "queue": QUEUE_BUILT_PIECES, "routing_keys": list(RK_BUILT_PIECES)},
            TOPIC_INFO,
        )

        await asyncio.Future()
    finally:
        await connection.close()


async def handle_built_piece(message) -> None:
    """Procesa una pieza fabricada: inserta WarehouseOrderPiece y recalcula finished.

    Estrategia:
        - Parse JSON -> PieceBuiltEvent
        - DB: recibir_pieza_fabricada() (con commit)
        - ACK automático al salir del context manager

    Nota:
        - Payload inválido: ACK y descartar.
        - Si la order no existe (race), requeue para reintentar.
    """
    async with message.process(requeue=True):
        payload = json.loads(message.body)

        # 1) Validación / mapping del payload
        try:
            event = _payload_to_piece_built_event(payload)
        except Exception as exc:
            logger.error("[WAREHOUSE] ❌ Payload inválido en built_piece: %s | payload=%s", exc, payload)
            await publish_to_logger(
                {"message": "Payload inválido en built_piece", "error": str(exc), "payload": payload},
                TOPIC_ERROR,
            )
            return  # ACK y descartado

        # 2) BD: registrar pieza + commit
        async with SessionLocal() as db:
            try:
                db_order, completed = await warehouse_service.recibir_pieza_fabricada(db, event)
                await db.commit()

                logger.info("[WAREHOUSE] ✅ Pieza registrada: order=%s type=%s status=%s", db_order.id, event.piece_type, db_order.status)
                await publish_to_logger(
                    {"message": "Pieza registrada", "order_id": int(db_order.id), "piece_type": str(event.piece_type), "status": str(db_order.status)},
                    TOPIC_INFO,
                )

                if completed:
                    logger.info("[WAREHOUSE] 🎉🎉🎉 Order %s COMPLETED ✅", db_order.id)

                    await publish_fabrication_completed(db_order.id)
                    
                    await publish_to_logger(
                        {"message": "Order COMPLETED", "order_id": int(db_order.id)},
                        TOPIC_INFO,
                    )

            except ValueError as exc:
                msg = str(exc)
                logger.warning("[WAREHOUSE] ⚠️ No se pudo registrar pieza: %s | payload=%s", msg, payload)
                await db.rollback()

                # Requeue SOLO si es el caso típico: pieza llega antes que la order
                if "no existe" in msg.lower():
                    raise  # requeue=True

                await publish_to_logger(
                    {"message": "Error registrando pieza", "error": msg, "payload": payload},
                    TOPIC_WARN,
                )
                return  # ACK y descartar

            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                logger.error("[WAREHOUSE] ❌ Error inesperado registrando pieza: %s", exc, exc_info=True)
                await publish_to_logger(
                    {"message": "Error inesperado registrando pieza", "error": str(exc), "payload": payload},
                    TOPIC_ERROR,
                )
                raise  # requeue=True


# =============================================================================
# 3) SAGA CANCELACIÓN: Order -> Warehouse -> Machine -> Warehouse -> Order
# =============================================================================
#region 2. ORDER CANCEL
async def consume_process_canceled_events() -> None:
    """Consume comando de cancelación enviado por Order.
    """
    logger.info("[WAREHOUSE] 🔄 Iniciando consume_process_canceled_events...")
    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange_saga_cancelation_commands(channel)

        queue = await channel.declare_queue(QUEUE_CANCEL_FABRICATION, durable=True)
        await queue.bind(exchange, routing_key=RK_CMD_CANCEL_FABRICATION)

        await queue.consume(handle_process_canceled)

        logger.info("[WAREHOUSE] 🟢 Escuchando cancelación en '%s' (rk=%s)", QUEUE_CANCEL_FABRICATION, RK_CMD_CANCEL_FABRICATION)
        await publish_to_logger(
            {"message": "Escuchando cmd.cancel_*", "queue": QUEUE_CANCEL_FABRICATION, "routing_keys": RK_CMD_CANCEL_FABRICATION},
            TOPIC_INFO,
        )

        await asyncio.Future()
    finally:
        await connection.close()


async def handle_process_canceled(message) -> None:
    """Handler del comando cmd.cancel_* (Order -> Warehouse).

    Payload esperado:
        {"order_id": int, "saga_id": str}

    Acciones:
        1) Validar payload (si es inválido -> ACK y descartar).
        2) BD:
            - comprobar order existe
            - marcar status=CANCELING
            - guardar cancel_saga_id
            - registrar fila de cancelación (idempotente)
        3) Publicar cmd.machine.cancel (hacia máquinas) [incluye saga_id si está]
        4) Commit
    """
    async with message.process(requeue=True):
        # 1) Validación estricta: payload inválido -> ACK y descartar
        try:
            payload = json.loads(message.body)
            order_id = int(payload["order_id"])
            saga_id = str(payload["saga_id"])
        except (json.JSONDecodeError, KeyError, TypeError, ValueError) as exc:
            logger.error("[WAREHOUSE] ❌ Payload inválido en cmd.cancel_*: %s | raw=%s", exc, message.body)
            await publish_to_logger(
                {"message": "Payload inválido en cmd.cancel_*", "error": str(exc), "raw": message.body.decode(errors="ignore")},
                TOPIC_ERROR,
            )
            return

        # 2) BD: aplicar CANCELING + registrar cancelación
        async with SessionLocal() as db:
            try:
                order = await crud.get_fabrication_order(db, order_id)
                if order is None:
                    logger.warning("[WAREHOUSE]  ⚠️ Cancelación para order inexistente: %s (saga=%s)", order_id, saga_id)
                    await publish_to_logger(
                        {"message": "Cancelación para order inexistente", "order_id": order_id, "saga_id": saga_id},
                        TOPIC_WARN,
                    )
                    return

                # Idempotencia / estados terminales
                if order.status in ("CANCELED", "COMPLETED"):
                    logger.info("[WAREHOUSE] ℹ️  Cancel ignorado: order=%s estado terminal=%s", order_id, order.status)
                    return

                order.status = "CANCELING"
                order.cancel_saga_id = saga_id
                await db.flush()

                await warehouse_service.registrar_cancelacion(db, order_id, saga_id)

                # 3) Publicar cancel a máquinas (incluye saga_id para trazabilidad)
                await publish_machine_cancel(order_id=order_id, saga_id=saga_id)

                # 4) Commit
                await db.commit()
                logger.warning("[WAREHOUSE] ⚠️  Cancelación iniciada: order=%s saga=%s -> cmd.machine.cancel publicado", order_id, saga_id)
                await publish_to_logger(
                    {"message": "Cancelación iniciada", "order_id": order_id, "saga_id": saga_id},
                    TOPIC_INFO,
                )

            except Exception as exc:  # noqa: BLE001
                await db.rollback()
                logger.error("[WAREHOUSE] ❌ Error procesando cmd.cancel_*: %s | payload=%s", exc, payload, exc_info=True)
                await publish_to_logger(
                    {"message": "Error procesando cmd.cancel_*", "error": str(exc), "payload": payload},
                    TOPIC_ERROR,
                )
                raise  # requeue=True

#region 2.1 cancel machine
async def consume_machine_canceled_events() -> None:
    """Consume evt.machine.canceled emitido por máquinas.

    Cuando Warehouse recibe confirmación suficiente, publica evt.*_canceled hacia Order.
    """
    logger.info("[WAREHOUSE] 🔄 Iniciando consume_machine_canceled_events...")
    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange(channel)

        queue = await channel.declare_queue(QUEUE_MACHINE_CANCELED, durable=True)
        await queue.bind(exchange, routing_key=RK_EVT_MACHINE_CANCELED)

        await queue.consume(handle_machine_canceled)

        logger.info("[WAREHOUSE] 🟢 Escuchando %s en '%s'", RK_EVT_MACHINE_CANCELED, QUEUE_MACHINE_CANCELED)
        await publish_to_logger(
            {"message": "Escuchando evt.machine.canceled", "queue": QUEUE_MACHINE_CANCELED},
            TOPIC_INFO,
        )

        await asyncio.Future()
    finally:
        await connection.close()


async def handle_machine_canceled(message) -> None:
    """Handler de evt.machine.canceled.

    Payload esperado (ideal):
        {"order_id": int, "machine": "A"|"B"}

    Compatibilidad:
        - Si no viene "machine", intenta inferirlo desde:
            * "piece_type" / "machine_type" / "type"
            * si no se puede inferir -> se ignora con warning (no requeue)
    """
    async with message.process(requeue=True):
        payload = json.loads(message.body)
        logger.info("[WAREHOUSE] 📥 Recibido evt.machine.canceled: payload=%s", payload)

        # Validación mínima
        if "order_id" not in payload:
            logger.warning("[WAREHOUSE] ⚠️ evt.machine.canceled inválido (sin order_id). payload=%s", payload)
            await publish_to_logger(
                {"message": "evt.machine.canceled inválido (sin order_id)", "payload": payload},
                TOPIC_WARN,
            )
            return

        order_id = _to_int(payload.get("order_id"), 0)
        if order_id <= 0:
            logger.warning("[WAREHOUSE] ⚠️ evt.machine.canceled inválido (order_id). payload=%s", payload)
            return

        machine_raw = payload.get("machine_type")

        # Ojo: NO filtramos aquí por ("A","B"). Deja que el servicio haga el normalizado:
        #   - "A"/"B"
        #   - "machine-a"/"machine-b"
        #   - None / "" / desconocido => confirmación global (por diseño)
        if machine_raw is None:
            machine_raw = ""
        if isinstance(machine_raw, str):
            machine_raw = machine_raw.strip().upper()
        else:
            machine_raw = str(machine_raw).strip().upper()

        if not machine_raw:
            logger.warning("[WAREHOUSE] ⚠️ evt.machine.canceled sin campo machine: se tratará como confirmación global. payload=%s", payload,)

        try:
            async with SessionLocal() as db:
                saga_id, done = await warehouse_service.confirmar_cancelacion_maquina(db, order_id, machine_raw)
                await db.commit()

        except ValueError as exc:
            logger.warning("[WAREHOUSE] ⚠️ evt.machine.canceled ignorado (%s). payload=%s", exc, payload)
            return
        except Exception as exc:  # noqa: BLE001
            logger.error("[WAREHOUSE] ❌ Error procesando evt.machine.canceled. payload=%s", payload, exc_info=True)
            raise

        # Si ya tenemos confirmación suficiente, aplicamos cancelación y avisamos a Order
        if done:
            async with SessionLocal() as db:
                order = await warehouse_service.aplicar_cancelacion_confirmada(db, order_id)
                saga_id = order.cancel_saga_id
                await db.commit()

            await publish_fabrication_canceled(order_id=order_id, saga_id=str(saga_id))


# =============================================================================
# 4) PUBLISHERS (máquinas + evento final cancelación)
# =============================================================================
#region 3. PUBLISHERS
async def publish_pieces_to_machines(piezas_a_fabricar: List[dict], order_date_iso: str) -> None:
    """Publica piezas individuales a máquinas A/B.

    Cada mensaje:
        - piece_id (uuid)
        - order_id
        - piece_type ("A"|"B")
        - order_date (ISO)

    Nota:
        - Si no hay nada que fabricar (stock suficiente), no publica nada.
    """
    if not piezas_a_fabricar:
        logger.info("[WAREHOUSE] ✅ Order cubierta por stock: no hay nada que publicar a máquinas.")
        await publish_to_logger({"message": "Order cubierta por stock (sin fabricación)"}, TOPIC_INFO)
        return

    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange(channel)

        for pieza in piezas_a_fabricar:
            piece_type = (pieza.get("piece_type") or "").upper()
            order_id = pieza.get("order_id")

            if piece_type not in ("A", "B"):
                raise ValueError(f"piece_type inválido en piezas_a_fabricar: {piece_type}")

            routing_key = RK_MACHINE_A if piece_type == "A" else RK_MACHINE_B

            msg_payload = {
                "piece_id": str(uuid.uuid4()),
                "order_id": _to_int(order_id, 0),
                "piece_type": piece_type,
                "order_date": order_date_iso,
            }

            await exchange.publish(_build_json_message(msg_payload), routing_key=routing_key)

        logger.info("[WAREHOUSE] 📤 Publicadas %s piezas a máquinas (A->%s, B->%s)", len(piezas_a_fabricar), RK_MACHINE_A, RK_MACHINE_B)
        await publish_to_logger(
            {"message": "Publicadas piezas a máquinas", "count": len(piezas_a_fabricar), "rk_a": RK_MACHINE_A, "rk_b": RK_MACHINE_B},
            TOPIC_INFO,
        )
    finally:
        await connection.close()

#region 3.1 order finished
async def publish_fabrication_completed(order_id: int) -> None:
    """
    Publica evento hacia Order indicando que la fabricación ha finalizado.

    Evento:
        routing_key = warehouse.fabrication.completed

    Payload mínimo (compatible con Order.handle_warehouse_event):
        {"order_id": int, "status": "completed"}

    Nota importante (robustez):
        - Si este publish falla, conviene lanzar excepción para que el mensaje original
          (piece.done o order.confirmed) se requeuee y se reintente.
    """
    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange(channel)

        payload = {"order_id": int(order_id), "status": "completed"}

        await exchange.publish(_build_json_message(payload), routing_key=RK_EVT_FABRICATION_COMPLETED)

        logger.info("[WAREHOUSE] 📤 %s → %s", RK_EVT_FABRICATION_COMPLETED, payload)
        await publish_to_logger(
            {"message": "Fabricación completada publicada", "order_id": int(order_id), "rk": RK_EVT_FABRICATION_COMPLETED},
            TOPIC_INFO,
        )
    finally:
        await connection.close()

#region 3.2 start cancel
async def publish_machine_cancel(order_id: int, saga_id: str | None = None) -> None:
    """Publica cmd.machine.cancel para que las máquinas detengan fabricación.

    Payload:
        {"order_id": int, "saga_id": str?}

    Nota:
        - Añadir saga_id es compatible (campo extra).
    """
    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange(channel)

        payload = {"order_id": int(order_id)}
        if saga_id:
            payload["saga_id"] = str(saga_id)

        await exchange.publish(_build_json_message(payload), routing_key=RK_CMD_MACHINE_CANCEL)
        logger.info("[WAREHOUSE] 📤 %s → %s", RK_CMD_MACHINE_CANCEL, payload)
    finally:
        await connection.close()

#region 3.3 cancel finished
async def publish_fabrication_canceled(order_id: int, saga_id: str) -> None:
    """Publica evento final de cancelación hacia Order.

    Publica:
        - evt.fabrication_canceled (canónico)
        - evt.fabrication_canceled (legacy/compat)

    Payload:
        {"order_id": int, "saga_id": str}
    """
    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange_saga_cancelation_events(channel)

        payload = {"order_id": int(order_id), "saga_id": str(saga_id)}

        await exchange.publish(_build_json_message(payload), routing_key=RK_EVT_FABRICATION_CANCELED)

        logger.info("[WAREHOUSE] 📤 Publicado cancel final (%s) → %s", RK_EVT_FABRICATION_CANCELED, payload)
        await publish_to_logger(
            {"message": "Cancelación confirmada publicada", "order_id": int(order_id), "saga_id": str(saga_id)},
            TOPIC_INFO,
        )
    finally:
        await connection.close()


# =============================================================================
# Auth
# =============================================================================
#region 4. AUTH
async def consume_auth_events() -> None:
    """
    Consume eventos sobre el estado de Auth.

    Cola:
        - Q_AUTH_EVENTS <- RK_AUTH_RUNNING
        - Q_AUTH_EVENTS <- RK_AUTH_NOT_RUNNING

    Nota:
        - Se usa una única cola con 2 bindings, como estaba.
    """
    _, channel = await get_channel()
    exchange = await declare_exchange(channel)

    order_queue = await channel.declare_queue(Q_AUTH_EVENTS, durable=True)
    await order_queue.bind(exchange, routing_key=RK_AUTH_RUNNING)
    await order_queue.bind(exchange, routing_key=RK_AUTH_NOT_RUNNING)

    await order_queue.consume(handle_auth_events)

    logger.info("[ORDER] 🟢 Escuchando eventos de Auth (%s / %s)...", RK_AUTH_RUNNING, RK_AUTH_NOT_RUNNING)
    await asyncio.Future()


async def handle_auth_events(message) -> None:
    """
    Gestiona eventos de auth.running / auth.not_running.

    Nota importante:
        - Aunque recibamos 'running', Auth puede no estar listo aún (FastAPI aún no sirve HTTP).
        - Por eso hacemos reintentos contra Consul (passing=true) y luego descargamos la clave.
    """
    async with message.process():
        data = json.loads(message.body)
        if data.get("status") != "running":
            return

        try:
            await _ensure_auth_public_key()
            await publish_to_logger(
                message={"message": "Clave pública guardada", "path": PUBLIC_KEY_PATH},
                topic=TOPIC_INFO,
            )
        except Exception as exc:
            logger.error("[WAREHOUSE] ❌ Error obteniendo clave pública: %s", exc)
            await publish_to_logger(
                message={"message": "Error clave pública", "error": str(exc)},
                topic=TOPIC_ERROR,
            )


# =============================================================================
# 5) LOGGER
# =============================================================================
#region 5. LOGGER
async def publish_to_logger(message: dict, topic: str) -> None:
    """Publica logs estructurados en el exchange de logs.

    Args:
        message: dict con datos del log.
        topic: "warehouse.info" | "warehouse.warn" | "warehouse.error"
    """
    connection = None
    try:
        connection, channel = await get_channel()
        exchange = await declare_exchange_logs(channel)

        severity = topic.split(".", 1)[1] if "." in topic else "info"
        log_data = {
            "measurement": "logs",
            "service": "warehouse",
            "severity": severity,
            **message,
        }

        await exchange.publish(_build_json_message(log_data), routing_key=topic)

    except Exception as exc:  # noqa: BLE001
        logger.error("[WAREHOUSE] ❌ Error publicando en logger: %s", exc, exc_info=True)
    finally:
        if connection:
            await connection.close()