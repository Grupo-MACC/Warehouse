# -*- coding: utf-8 -*-
"""Utilidad para declarar colas e intercambios de warehouse en RabbitMQ.

Este módulo se puede ejecutar al arrancar el servicio para asegurarse
de que exchange y colas existen con los bindings adecuados.
"""

from microservice_chassis_grupo2.core.rabbitmq_core import get_channel, declare_exchange, declare_exchange_saga, declare_exchange_command

async def setup_rabbitmq():
    try:
        connection, channel = await get_channel()
        
        exchange = await declare_exchange()
        exchange_saga = await declare_exchange_saga()
        exchange_command = await declare_exchange_command()

        payment_queue = await channel.declare_queue('payment_queue', durable=True)

        await payment_queue.bind(exchange, routing_key='order.created')

        return_money_queue = await channel.declare_queue('return_money_queue', durable=True)
        await return_money_queue.bind(exchange_command, routing_key='return.money')

        pay_queue = await channel.declare_queue('pay_queue', durable=True)
        await pay_queue.bind(exchange_command, routing_key='pay')

        print("✅ RabbitMQ configurado correctamente (exchange + colas creadas).")
    except Exception as exc:
        raise exc
    finally:
        connection.close()
