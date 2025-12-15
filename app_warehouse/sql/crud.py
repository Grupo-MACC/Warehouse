# -*- coding: utf-8 -*-
"""Funciones CRUD para el microservicio de almacén."""

import logging
from typing import List

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from . import models, schemas

logger = logging.getLogger(__name__)


async def list_items(db: AsyncSession, limit: int = 100) -> List[models.WarehouseItem]:
    """Devuelve una lista de entradas de almacén.

    :param db: Sesión asíncrona de base de datos.
    :param limit: Número máximo de registros a devolver.
    :return: Lista de instancias de ``WarehouseItem``.
    """
    stmt = select(models.WarehouseItem).limit(limit)
    result = await db.execute(stmt)
    items = result.scalars().all()
    return list(items)


async def create_item(
    db: AsyncSession, item_in: schemas.WarehouseItemCreate
) -> models.WarehouseItem:
    """Crea una nueva entrada de almacén.

    :param db: Sesión asíncrona de base de datos.
    :param item_in: Datos de la entrada a crear.
    :return: Instancia de ``WarehouseItem`` recién creada.
    """
    item = models.WarehouseItem(
        process_id=item_in.process_id,
        piece_type=item_in.piece_type,
        quantity=item_in.quantity,
    )
    db.add(item)
    await db.commit()
    await db.refresh(item)
    logger.info(
        "[WAREHOUSE] 📦 Entrada creada: process_id=%s piece_type=%s quantity=%s",
        item.process_id,
        item.piece_type,
        item.quantity,
    )
    return item