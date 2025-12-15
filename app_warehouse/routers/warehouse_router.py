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


@router.get(
    "/items",
    response_model=List[schemas.WarehouseItem],
    summary="Lista el inventario actual del almacén",
    tags=["Warehouse"],
)
async def list_items(
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
) -> List[schemas.WarehouseItem]:
    """Devuelve todas las entradas de almacén registradas.

    Este endpoint devuelve la vista "cruda" del almacén. En iteraciones
    posteriores podrás añadir filtros (por tipo de pieza, por proceso, etc.)
    o paginación.
    """
    logger.debug("[WAREHOUSE] GET /warehouse/items by user_id=%s", user_id)
    items = await crud.list_items(db)
    return items


@router.post(
    "/items",
    response_model=schemas.WarehouseItem,
    status_code=status.HTTP_201_CREATED,
    summary="Registra piezas procedentes de un proceso cancelado",
    tags=["Warehouse"],
)
async def create_item(
    item_in: schemas.WarehouseItemCreate,
    db: AsyncSession = Depends(get_db),
    user_id: int = Depends(get_current_user),
) -> schemas.WarehouseItem:
    """Crea una nueva entrada en el almacén.

    Este endpoint representa el *intake* de piezas: normalmente será invocado
    cuando un proceso de fabricación se cancela y hay piezas ya fabricadas
    que deben almacenarse.
    """
    logger.info(
        "[WAREHOUSE] POST /warehouse/items by user_id=%s process_id=%s piece_type=%s quantity=%s",
        user_id,
        item_in.process_id,
        item_in.piece_type,
        item_in.quantity,
    )
    item = await crud.create_item(db, item_in)
    return item
