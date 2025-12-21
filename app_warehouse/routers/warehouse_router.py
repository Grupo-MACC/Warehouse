# -*- coding: utf-8 -*-
"""Definición de rutas del microservicio de almacén (warehouse)."""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from microservice_chassis_grupo2.core.dependencies import (
    get_db,
    get_current_user,
    check_public_key,
)

from sql import crud, schemas

logger = logging.getLogger(__name__)

router = APIRouter(
    prefix="/warehouse",
)


@router.get(
    "/health",
    response_model=schemas.Message,
    summary="Health check del microservicio de almacén",
)
async def health_check() -> schemas.Message:
    """Verifica que el servicio está vivo y que la clave pública es válida.

    Se apoya en la función ``check_public_key`` del *chassis*. Si la clave no
    es válida, responde con un HTTP 503 para que Consul marque el servicio
    como no saludable.
    """
    logger.debug("[WAREHOUSE] GET /warehouse/health")
    if check_public_key():
        return schemas.Message(detail="OK")

    raise HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Service not available",
    )
