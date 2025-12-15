# -*- coding: utf-8 -*-
"""Servicios de integración de warehouse con RabbitMQ.

Este módulo define las corrutinas que se conectan a RabbitMQ, declaran
colas y consumen eventos relacionados con el almacén.
"""

import asyncio
import json
import logging

from aio_pika import Message  # Podrás usarlo más adelante para publicar eventos.
from microservice_chassis_grupo2.core.rabbitmq_core import (
    get_channel,
    declare_exchange,
    declare_exchange_logs,
)

logger = logging.getLogger(__name__)


async def consume_process_canceled_events():
    """Consume eventos de procesos cancelados desde RabbitMQ.

    Esta corrutina:
    - Abre un canal contra RabbitMQ (via `get_channel`).
    - Declara el exchange principal (topic).
    - Declara y enlaza la cola `process_canceled_queue` con la routing key
      `process.canceled`.
    - Asocia la cola con la función `handle_process_canceled`.
    - Se queda bloqueada para escuchar eventos indefinidamente.
    """
    try:
        logger.info("[WAREHOUSE] 🔄 Iniciando consume_process_canceled_events...")
        print("[WAREHOUSE] 🔄 Iniciando consume_process_canceled_events...", flush=True)

        # Obtenemos conexión y canal al broker
        _, channel = await get_channel()

        # Exchange principal (mismo patrón que delivery)
        exchange = await declare_exchange(channel)

        # Cola donde esperamos que otro servicio publique `process.canceled`
        queue = await channel.declare_queue("process_canceled_queue", durable=True)
        await queue.bind(exchange, routing_key="process.canceled")

        # Registramos el callback
        await queue.consume(handle_process_canceled)

        logger.info("[WAREHOUSE] 🟢 Escuchando eventos process.canceled...")
        print("[WAREHOUSE] 🟢 Escuchando eventos process.canceled...", flush=True)

        # Mantener la corrutina viva
        await asyncio.Future()

    except Exception as exc:  # noqa: BLE001
        logger.error(
            "[WAREHOUSE] ❌ Error en consume_process_canceled_events: %s",
            exc,
            exc_info=True,
        )
        print(f"[WAREHOUSE] ❌ Error en consume_process_canceled_events: {exc}", flush=True)


async def handle_process_canceled(message):
    """Procesa un evento de proceso cancelado.

    La estructura exacta del mensaje dependerá de cómo lo publique el
    microservicio de procesos/máquinas. Para el primer commit, nos
    limitamos a:
    - Parsear el body como JSON.
    - Loguear el contenido.
    - Dejar un TODO para, en siguientes pasos, llamar a la capa de
      negocio/CRUD de warehouse.
    """
    async with message.process():
        try:
            # Decodificar el mensaje recibido
            data = json.loads(message.body)

            # Ejemplo de estructura esperada (ajustable más adelante):
            # {
            #   "process_id": 123,
            #   "piece_type": "A",
            #   "quantity": 10
            # }
            process_id = data.get("process_id")
            piece_type = data.get("piece_type")
            quantity = data.get("quantity")

            logger.info(
                "[WAREHOUSE] 📥 Proceso cancelado recibido: process_id=%s "
                "piece_type=%s quantity=%s",
                process_id,
                piece_type,
                quantity,
            )

            # 🔧 Aquí, en iteraciones futuras:
            # - Llamar a un servicio/CRUD para registrar las piezas en almacén.
            #   Ejemplo:
            #   await warehouse_service.store_canceled_pieces(
            #       process_id=process_id,
            #       piece_type=piece_type,
            #       quantity=quantity,
            #   )

        except Exception as exc:  # noqa: BLE001
            logger.error(
                "[WAREHOUSE] ❌ Error procesando evento process.canceled: %s",
                exc,
                exc_info=True,
            )
            await publish_to_logger(
                message={"message": "Error procesando process.canceled", "error": str(exc)},
                topic="warehouse.error",
            )


async def publish_to_logger(message: dict, topic: str):
    """Publica mensajes de log en el exchange de logs.

    Esto permite integrar los logs de warehouse en el sistema de logging
    centralizado del proyecto, igual que hace delivery.
    """
    connection = None
    try:
        # Abrimos conexión y canal contra el broker
        connection, channel = await get_channel()

        # Declaramos/obtenemos el exchange de logs
        exchange = await declare_exchange_logs(channel)

        # Serializamos el mensaje a JSON
        body = json.dumps(message).encode()

        # Construimos el mensaje RabbitMQ persistente
        msg = Message(
            body=body,
            content_type="application/json",
            delivery_mode=2,  # persistente
        )

        # Publicamos usando el topic proporcionado
        await exchange.publish(message=msg, routing_key=topic)

    except Exception as exc:  # noqa: BLE001
        logger.error("[WAREHOUSE] ❌ Error publicando en logger: %s", exc, exc_info=True)
    finally:
        if connection:
            await connection.close()
