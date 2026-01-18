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
from broker import warehouse_broker_service
from consul_client import get_consul_client

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
    consul = get_consul_client()

    try:
        logger.info("Starting up")
        
        # Registro "auto" (usa SERVICE_* y CONSUL_* desde entorno)
        ok = await consul.register_self()
        logger.info("✅ Consul register_self: %s", ok)

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
            task_auth = asyncio.create_task(warehouse_broker_service.consume_auth_events())
            task_order = asyncio.create_task(warehouse_broker_service.consume_incoming_orders())
            task_order_cancel = asyncio.create_task(warehouse_broker_service.consume_process_canceled_events())
            task_machine = asyncio.create_task(warehouse_broker_service.consume_built_pieces())
            task_machine_canceled = asyncio.create_task(warehouse_broker_service.consume_machine_canceled_events()) 

        except Exception as e:
            logger.error(f"❌ Error lanzando broker service: {e}", exc_info=True)

        # Dejar que la app FastAPI viva
        yield

    finally:
        logger.info("Shutting down database")
        await database.engine.dispose()
        logger.info("Shutting down rabbitmq")
        task_auth.cancel()
        task_order.cancel()
        task_order_cancel.cancel()
        task_machine.cancel()
        task_machine_canceled.cancel()

        # Deregistro (auto) + cierre del cliente HTTP
        try:
            ok = await consul.deregister_self()
            logger.info("✅ Consul deregister_self: %s", ok)
        except Exception:
            logger.exception("Error desregistrando en Consul")

        try:
            await consul.aclose()
        except Exception:
            logger.exception("Error cerrando cliente Consul")


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
    """
    Application entry point. Starts the Uvicorn server with SSL configuration.
    Runs the FastAPI application on host.
    """
    cert_file = os.getenv("SERVICE_CERT_FILE", "/certs/warehouse/warehouse-cert.pem")
    key_file = os.getenv("SERVICE_KEY_FILE", "/certs/warehouse/warehouse-key.pem")

    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("SERVICE_PORT", "5005")),
        reload=True,
        ssl_certfile=cert_file,
        ssl_keyfile=key_file,
    )
