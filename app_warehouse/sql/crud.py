# -*- coding: utf-8 -*-
"""CRUD y helpers de BD para Warehouse.

Este módulo define las funciones CRUD y helpers para interactuar con la
base de datos del microservicio de almacén (warehouse).

- Gestión de stock disponible por tipo de pieza.
- Gestión de orders en fabricación.
- Gestión de piezas asociadas a orders.

Nota: todas las funciones son asíncronas y usan AsyncSession de SQLAlchemy.
"""

import logging
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from . import models

logger = logging.getLogger(__name__)


# ----------------------------- STOCK ----------------------------------------------------------------

async def get_stock_by_type(db: AsyncSession, piece_type: str) -> Optional[models.WarehouseStock]:
    """Obtiene la fila de stock para un tipo de pieza (A/B)."""
    stmt = select(models.WarehouseStock).where(models.WarehouseStock.piece_type == piece_type)
    result = await db.execute(stmt)
    return result.scalars().first()


async def get_or_create_stock_row(db: AsyncSession, piece_type: str) -> models.WarehouseStock:
    """Garantiza que existe una fila de stock para el tipo A/B.

    Si no existe, se crea con quantity=0.
    """
    row = await get_stock_by_type(db, piece_type)
    if row is not None:
        return row

    row = models.WarehouseStock(piece_type=piece_type, quantity=0)
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


async def change_stock(db: AsyncSession, piece_type: str, delta: int) -> models.WarehouseStock:
    """Incrementa/decrementa stock disponible.

    Reglas:
    - No permitimos stock negativo.
    - Este método hace commit para ser robusto si se llama desde consumers más adelante.
    """
    row = await get_or_create_stock_row(db, piece_type)
    new_qty = row.quantity + delta

    if new_qty < 0:
        raise ValueError(f"Stock negativo para tipo {piece_type}: {row.quantity} + ({delta})")

    row.quantity = new_qty
    await db.commit()
    await db.refresh(row)
    return row


# ----------------------------- ORDER EN FABRICACIÓN -------------------------------------------------

async def get_manufacturing_order(db: AsyncSession, order_id: int) -> Optional[models.WarehouseManufacturingOrder]:
    """Obtiene la order en fabricación por id."""
    return await db.get(models.WarehouseManufacturingOrder, order_id)


async def create_manufacturing_order(
    db: AsyncSession,
    order_id: int,
    total_a: int,
    total_b: int,
    to_build_a: int,
    to_build_b: int,
    finished: bool,
) -> models.WarehouseManufacturingOrder:
    """Crea una order en fabricación.

    Nota:
    - Usamos order_id como PK para alinear con el ecosistema (menos mapeos).
    """
    order = models.WarehouseManufacturingOrder(
        id=order_id,
        total_a=total_a,
        total_b=total_b,
        to_build_a=to_build_a,
        to_build_b=to_build_b,
        finished=finished,
    )
    db.add(order)
    await db.commit()
    await db.refresh(order)
    return order


async def set_order_finished(db: AsyncSession, order_id: int, finished: bool) -> Optional[models.WarehouseManufacturingOrder]:
    """Marca una order como finished=True/False."""
    order = await get_manufacturing_order(db, order_id)
    if order is None:
        return None

    order.finished = finished
    await db.commit()
    await db.refresh(order)
    return order


# ----------------------------- PIEZAS POR ORDER -----------------------------------------------------

async def create_order_piece(
    db: AsyncSession,
    order_id: int,
    piece_type: str,
    source: str,
    manufacturing_date=None,
) -> models.WarehouseOrderPiece:
    """Inserta una pieza asociada a una order.

    source:
    - 'stock'
    - 'manufactured'
    """
    piece = models.WarehouseOrderPiece(
        order_id=order_id,
        piece_type=piece_type,
        source=source,
        manufacturing_date=manufacturing_date,
    )
    db.add(piece)
    await db.commit()
    await db.refresh(piece)
    return piece


async def count_order_pieces_by_type(db: AsyncSession, order_id: int, piece_type: str) -> int:
    """Cuenta cuántas piezas de un tipo hay registradas para una order."""
    stmt = select(models.WarehouseOrderPiece).where(
        models.WarehouseOrderPiece.order_id == order_id,
        models.WarehouseOrderPiece.piece_type == piece_type,
    )
    result = await db.execute(stmt)
    return len(result.scalars().all())
