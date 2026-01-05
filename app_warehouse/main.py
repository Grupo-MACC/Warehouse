# -*- coding: utf-8 -*-
"""Punto de entrada del microservicio de almacén (warehouse)."""

import logging.config
import os
from contextlib import asynccontextmanager
import asyncio

import uvicorn
from fastapi import FastAPI

from routers import warehouse_router
from microservice_chassis_grupo2.sql import database, models
from broker import warehouse_broker_service, setup_rabbitmq
from consul_client import create_consul_client

# logging.config.fileConfig(os.path.join(os.path.dirname(__file__), "logging.ini"))
logging.config.fileConfig(os.path.join(os.path.dirname(__file__), "logging.ini"),disable_existing_loggers=False,)
logger = logging.getLogger(__name__)

APP_VERSION = os.getenv("APP_VERSION", "1.0.0")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gestiona el ciclo de vida de la aplicación warehouse.

    - Registra el servicio en Consul.
    - Crea las tablas de base de datos (usando el Base del chassis).
    - Configura RabbitMQ (exchange + colas específicas de warehouse).
    - Lanza el consumer de eventos `process.canceled`.
    - Libera recursos al apagar (DB + tasks + Consul).
    """
    consul_client = create_consul_client()

    service_id = os.getenv("SERVICE_ID", "warehouse-1")
    service_name = os.getenv("SERVICE_NAME", "warehouse")
    service_port = int(os.getenv("SERVICE_PORT", "5005"))


    try:
        logger.info("[WAREHOUSE] 🚀 Arrancando servicio de almacén")

        # Registro en Consul
        result = await consul_client.register_service(
            service_name=service_name,
            service_id=service_id,
            service_port=service_port,
            service_address=service_name,
            tags=["fastapi", service_name],
            meta={"version": APP_VERSION},
            health_check_url=f"http://{service_name}:{service_port}/warehouse/health",
        )
        logger.info("[WAREHOUSE] ✅ Registro en Consul: %s", result)

        # Creación de tablas
        try:
            logger.info("[WAREHOUSE] 🗄️ Creando tablas de base de datos")
            async with database.engine.begin() as conn:
                await conn.run_sync(models.Base.metadata.create_all)
        except Exception as exc:
            logger.exception("[WAREHOUSE] ❌ Error creando tablas: %s", exc)

        # Configuración de RabbitMQ (colas/bindings)        
        try:
            logger.info("🚀 Lanzando tasks de RabbitMQ consumers...")
            task_order = asyncio.create_task(warehouse_broker_service.consume_incoming_orders())
            task_order_cancel = asyncio.create_task(warehouse_broker_service.consume_process_canceled_events())
            task_machine = asyncio.create_task(warehouse_broker_service.consume_built_pieces())
            task_machine_canceled = asyncio.create_task(warehouse_broker_service.consume_machine_canceled_events()) 

            logger.info("✅ Tasks de RabbitMQ creados correctamente")
        except Exception as e:
            logger.error(f"❌ Error lanzando broker service: {e}", exc_info=True)

        # Dejar que la app FastAPI viva
        yield

    finally:
        logger.info("Shutting down database")
        await database.engine.dispose()
        logger.info("Shutting down rabbitmq")
        task_order.cancel()
        task_order_cancel.cancel()
        task_machine.cancel()
        task_machine_canceled.cancel()

        # Desregistro en Consul
        try:
            result = await consul_client.deregister_service(service_id)
            logger.info("[WAREHOUSE] ✅ Desregistro de Consul: %s", result)
        except Exception as exc:
            logger.warning("[WAREHOUSE] ⚠️ Error desregistrando en Consul: %s", exc)


app = FastAPI(
    redoc_url=None,
    version=APP_VERSION,
    servers=[{"url": "/", "description": "Development"}],
    license_info={
        "name": "MIT License",
        "url": "https://choosealicense.com/licenses/mit/",
    },
    lifespan=lifespan,
)

app.include_router(warehouse_router.router)

if __name__ == "__main__":
    uvicorn.run("main:app", host="0.0.0.0", port=5005, reload=True)
