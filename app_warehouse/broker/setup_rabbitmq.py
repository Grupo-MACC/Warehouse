# -*- coding: utf-8 -*-
"""
Declaración de exchanges/colas para el microservicio Warehouse.

Diseño:
    - Se definen constantes globales (sin depender de variables de entorno).
    - Se separa claramente:
        - nombre de cola (queue name)
        - routing key (routing key)
    - Esto evita confusiones y alinea RabbitMQ con los consumers del servicio.
"""

import logging
from microservice_chassis_grupo2.core.rabbitmq_core import get_channel, declare_exchange

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------
# Colas + routing keys (config estática)
# ---------------------------------------------------------------------

# Orders -> Warehouse
WAREHOUSE_ORDER_QUEUE: str = "warehouse_order_queue"
WAREHOUSE_ORDER_ROUTING_KEYS: tuple[str, ...] = ("warehouse.order", "order.created")

# Machines (comandos hacia máquinas)
MACHINE_A_QUEUE: str = "machine_a_queue"
MACHINE_B_QUEUE: str = "machine_b_queue"
MACHINE_A_ROUTING_KEY: str = "machine.a"
MACHINE_B_ROUTING_KEY: str = "machine.b"

# Pieces done -> Warehouse
WAREHOUSE_BUILT_QUEUE: str = "warehouse_built_queue"
WAREHOUSE_BUILT_ROUTING_KEYS: tuple[str, ...] = ("piece.done",)

# Cancel manufacturing (Order -> Warehouse)
WAREHOUSE_CANCEL_QUEUE: str = "warehouse_cancel_queue"
RK_CMD_CANCEL_MFG: str = "cmd.cancel_manufacturing"

# Machine canceled (Machines -> Warehouse) (si ya lo consumes)
WAREHOUSE_MACHINE_CANCELED_QUEUE: str = "warehouse_machine_canceled_queue"
RK_EVT_MACHINE_CANCELED: str = "evt.machine.canceled"


async def setup_rabbitmq():
    """
    Configura RabbitMQ (exchange + colas + bindings) para Warehouse.

    Declara:
        - Cola de orders (Warehouse)
        - Colas de máquinas A/B
        - Cola de piezas fabricadas
        - Cola de cancelación de fabricación
        - Cola de confirmación de cancelación por máquina (si aplica)

    Nota:
        - Esto no “impone” el exchange name: lo gestiona declare_exchange(channel).
        - declare_queue es idempotente: si la cola existe, no la rompe.
    """
    connection, channel = await get_channel()
    try:
        exchange = await declare_exchange(channel)

        # -------------------------------------------------------------
        # Declaración de colas (durables)
        # -------------------------------------------------------------
        order_queue = await channel.declare_queue(WAREHOUSE_ORDER_QUEUE, durable=True)
        machine_a_queue = await channel.declare_queue(MACHINE_A_QUEUE, durable=True)
        machine_b_queue = await channel.declare_queue(MACHINE_B_QUEUE, durable=True)
        built_queue = await channel.declare_queue(WAREHOUSE_BUILT_QUEUE, durable=True)

        # Cancelación (Order -> Warehouse)
        cancel_queue = await channel.declare_queue(WAREHOUSE_CANCEL_QUEUE, durable=True)

        # Confirmación cancelación (Machines -> Warehouse) (opcional pero recomendable)
        machine_canceled_queue = await channel.declare_queue(
            WAREHOUSE_MACHINE_CANCELED_QUEUE, durable=True
        )

        # -------------------------------------------------------------
        # Bindings
        # -------------------------------------------------------------
        for rk in WAREHOUSE_ORDER_ROUTING_KEYS:
            await order_queue.bind(exchange, routing_key=rk)

        await machine_a_queue.bind(exchange, routing_key=MACHINE_A_ROUTING_KEY)
        await machine_b_queue.bind(exchange, routing_key=MACHINE_B_ROUTING_KEY)

        for rk in WAREHOUSE_BUILT_ROUTING_KEYS:
            await built_queue.bind(exchange, routing_key=rk)

        # Cancel command
        await cancel_queue.bind(exchange, routing_key=RK_CMD_CANCEL_MFG)

        # Machine canceled events (si tus máquinas lo emiten)
        await machine_canceled_queue.bind(exchange, routing_key=RK_EVT_MACHINE_CANCELED)

        logger.info(
            "✅ RabbitMQ configurado: exchange + colas + bindings "
            "(orders, machines, built pieces, cancel manufacturing, machine canceled)."
        )

    finally:
        await connection.close()
