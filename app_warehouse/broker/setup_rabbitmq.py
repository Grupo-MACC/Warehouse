# -*- coding: utf-8 -*-
"""Declaración de exchanges/colas para el microservicio Warehouse.
"""

import os

from microservice_chassis_grupo2.core.rabbitmq_core import get_channel, declare_exchange


async def setup_rabbitmq():
    """Configura RabbitMQ (exchange + colas + bindings) para Warehouse.

    Variables de entorno soportadas:
        - WAREHOUSE_ORDER_QUEUE: nombre de la cola de entrada de orders.
        - WAREHOUSE_ORDER_ROUTING_KEYS: routing keys (CSV) que enlazan esa cola al exchange.
        - MACHINE_A_QUEUE / MACHINE_B_QUEUE: colas de las máquinas A/B.
        - MACHINE_A_ROUTING_KEY / MACHINE_B_ROUTING_KEY: routing keys hacia esas colas.
        - WAREHOUSE_BUILT_QUEUE: cola de piezas fabricadas.
        - WAREHOUSE_BUILT_ROUTING_KEYS: routing keys (CSV) que enlazan esa cola al exchange.

    Defaults recomendados (si no defines nada):
        - warehouse_order_queue  (bind: warehouse.order y order.created)
        - machine_a_queue        (bind: machine.a)
        - machine_b_queue        (bind: machine.b)
        - warehouse_built_queue  (bind: piece.done)
    """
    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange(channel)

        order_queue_name = os.getenv("WAREHOUSE_ORDER_QUEUE", "warehouse_order_queue")
        order_routing_keys_csv = os.getenv("WAREHOUSE_ORDER_ROUTING_KEYS", "warehouse.order,order.created")
        order_routing_keys = [rk.strip() for rk in order_routing_keys_csv.split(",") if rk.strip()]

        machine_a_queue_name = os.getenv("MACHINE_A_QUEUE", "machine_a_queue")
        machine_b_queue_name = os.getenv("MACHINE_B_QUEUE", "machine_b_queue")
        machine_a_routing_key = os.getenv("MACHINE_A_ROUTING_KEY", "machine.a")
        machine_b_routing_key = os.getenv("MACHINE_B_ROUTING_KEY", "machine.b")

        built_queue_name = os.getenv("WAREHOUSE_BUILT_QUEUE", "warehouse_built_queue")
        built_routing_keys_csv = os.getenv("WAREHOUSE_BUILT_ROUTING_KEYS", "piece.done")
        built_routing_keys = [rk.strip() for rk in built_routing_keys_csv.split(",") if rk.strip()]

        # ---- Declarar colas ---------------------------------------------------
        order_queue = await channel.declare_queue(order_queue_name, durable=True)
        machine_a_queue = await channel.declare_queue(machine_a_queue_name, durable=True)
        machine_b_queue = await channel.declare_queue(machine_b_queue_name, durable=True)

        built_queue = await channel.declare_queue(built_queue_name, durable=True)

        # Cola para process.canceled
        process_canceled_queue = await channel.declare_queue("process_canceled_queue", durable=True)

        # ---- Bindings ---------------------------------------------------------
        for rk in order_routing_keys:
            await order_queue.bind(exchange, routing_key=rk)

        await machine_a_queue.bind(exchange, routing_key=machine_a_routing_key)
        await machine_b_queue.bind(exchange, routing_key=machine_b_routing_key)

        for rk in built_routing_keys:
            await built_queue.bind(exchange, routing_key=rk)

        await process_canceled_queue.bind(exchange, routing_key="process.canceled")

        print("✅ RabbitMQ configurado correctamente (warehouse exchange + colas + bindings).")

    finally:
        await connection.close()
