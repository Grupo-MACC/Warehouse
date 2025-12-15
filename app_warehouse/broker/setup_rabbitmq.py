# -*- coding: utf-8 -*-
"""Utilidad para declarar colas e intercambios de warehouse en RabbitMQ.

Este módulo se puede ejecutar al arrancar el servicio para asegurarse
de que exchange y colas existen con los bindings adecuados.
"""

from microservice_chassis_grupo2.core.rabbitmq_core import (
    get_channel,
    declare_exchange,
)


async def setup_rabbitmq():
    """Declara el exchange principal y la cola de procesos cancelados.

    - Crea el exchange "principal" (topic), reutilizando la función
      `declare_exchange` del chassis.
    - Declara la cola `process_canceled_queue`.
    - La enlaza con la routing key `process.canceled`.
    """
    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange(channel)

        process_canceled_queue = await channel.declare_queue(
            "process_canceled_queue",
            durable=True,
        )
        await process_canceled_queue.bind(exchange, routing_key="process.canceled")

        print("✅ RabbitMQ (warehouse) configurado: process_canceled_queue creada y enlazada.")

    finally:
        # Cerramos la conexión al broker
        await connection.close()
