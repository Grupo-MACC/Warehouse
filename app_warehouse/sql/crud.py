# -*- coding: utf-8 -*-
"""CRUD y helpers de BD para Warehouse.

Filosofía (importante):
- Estas funciones NO hacen commit().
- Se asume que la transacción la controla quien llama (router/service),
  normalmente vía get_db() del chasis, que hace commit/rollback al final.

Esto permite que operaciones compuestas (descontar stock + crear order + insertar piezas)
sean atómicas.
"""

import logging
from typing import Optional

from sqlalchemy import func, delete
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.future import select

from . import models

logger = logging.getLogger(__name__)


# ----------------------------- STOCK (disponible) -------------------------------------------------
#region stock
async def get_stock_by_type(db: AsyncSession, piece_type: str) -> Optional[models.WarehouseStock]:
    """Obtiene la fila de stock para un tipo de pieza ('A' o 'B')."""
    stmt = select(models.WarehouseStock).where(models.WarehouseStock.piece_type == piece_type)
    result = await db.execute(stmt)
    return result.scalars().first()


async def get_or_create_stock_row(db: AsyncSession, piece_type: str) -> models.WarehouseStock:
    """Garantiza que existe una fila de stock para el tipo A/B.

    Si no existe, la crea con quantity=0.
    """
    row = await get_stock_by_type(db, piece_type)
    if row is not None:
        return row

    row = models.WarehouseStock(piece_type=piece_type, quantity=0)
    db.add(row)
    # Flush para que la fila exista dentro de la misma transacción
    await db.flush()
    return row


async def consume_stock(db: AsyncSession, piece_type: str, requested_qty: int) -> int:
    """Consume stock disponible para cubrir una demanda.

    Args:
        db: Sesión async.
        piece_type: 'A' o 'B'.
        requested_qty: cantidad solicitada (>=0).

    Returns:
        int: cantidad realmente consumida (0..requested_qty).

    Reglas:
    - Nunca deja stock negativo.
    - Si no hay stock, consume 0.
    """
    if requested_qty <= 0:
        return 0

    row = await get_or_create_stock_row(db, piece_type)
    used = min(row.quantity, requested_qty)
    row.quantity -= used

    logger.info("[WAREHOUSE][STOCK] Consumed=%s type=%s new_qty=%s", used, piece_type, row.quantity)
    return used


async def add_stock(db: AsyncSession, piece_type: str, delta: int):
    """Suma stock (sin commit)."""
    row = await get_or_create_stock_row(db, piece_type)
    row.quantity += delta
    await db.flush()
    return row


async def set_stock(db: AsyncSession, piece_type: str, quantity: int):
    """Fija stock (sin commit)."""
    row = await get_or_create_stock_row(db, piece_type)
    row.quantity = quantity
    await db.flush()
    return row


# ----------------------------- ORDER EN FABRICACIÓN ------------------------------------------------
#region order
async def get_manufacturing_order(db: AsyncSession, order_id: int) -> Optional[models.WarehouseOrder]:
    """Obtiene la order en fabricación por id."""
    return await db.get(models.WarehouseOrder, order_id)


async def create_manufacturing_order(
    db: AsyncSession,
    order_id: int,
    total_a: int,
    total_b: int,
    to_build_a: int,
    to_build_b: int,
    status: str = models.WAREHOUSE_ORDER_STATUS_IN_MANUFACTURING,
) -> models.WarehouseOrder:
    """Crea una order en fabricación.

    Nota:
        - No hace commit().
        - El estado final COMPLETED se decide por el recálculo (piezas registradas vs total).
    """
    order = models.WarehouseOrder(
        id=order_id,
        total_a=total_a,
        total_b=total_b,
        to_build_a=to_build_a,
        to_build_b=to_build_b,
        status=status,
    )
    db.add(order)
    await db.flush()
    return order



async def set_order_finished(db: AsyncSession, order_id: int, finished: bool) -> Optional[models.WarehouseOrder]:
    """Marca una order como finished=True/False."""
    order = await get_manufacturing_order(db, order_id)
    if order is None:
        return None

    order.finished = finished
    await db.flush()
    return order


# ----------------------------- PIEZAS POR ORDER ----------------------------------------------------
#region pieces
async def create_order_piece(
    db: AsyncSession,
    order_id: int,
    piece_type: str,
    source: str,
    manufacturing_date=None,
) -> models.WarehouseOrderPiece:
    """Inserta UNA pieza asociada a una order."""
    piece = models.WarehouseOrderPiece(
        order_id=order_id,
        piece_type=piece_type,
        source=source,
        manufacturing_date=manufacturing_date,
    )
    db.add(piece)
    await db.flush()
    return piece


async def count_order_pieces_by_type(db: AsyncSession, order_id: int, piece_type: str) -> int:
    """Cuenta cuántas piezas de un tipo hay registradas para una order (COUNT(*) real)."""
    stmt = select(func.count(models.WarehouseOrderPiece.id)).where(
        models.WarehouseOrderPiece.order_id == order_id,
        models.WarehouseOrderPiece.piece_type == piece_type,
    )
    result = await db.execute(stmt)
    return int(result.scalar() or 0)

# ----------------------------- CANCELACIONES DE FABRICACIÓN ---------------------------------------
#region order cancel
async def get_cancel(db, order_id: int):
    """
    Recupera cancelación registrada para un order_id.
    """
    stmt = select(models.WarehouseManufacturingCancellation).where(
        models.WarehouseManufacturingCancellation.order_id == order_id
    )
    res = await db.execute(stmt)
    return res.scalars().first()


async def create_cancel(db, order_id: int, saga_id: str):
    """
    Inserta un nuevo registro de cancelación.
    """
    row = models.WarehouseManufacturingCancellation(order_id=order_id, saga_id=saga_id)
    db.add(row)
    await db.flush()
    return row

#region Helper
async def get_piece_counts_for_order(db: AsyncSession, order_id: int) -> dict[str, int]:
    """Cuenta piezas A/B registradas para una order."""
    count_a = await count_order_pieces_by_type(db, order_id, "A")
    count_b = await count_order_pieces_by_type(db, order_id, "B")
    return {"A": count_a, "B": count_b}


async def delete_order_pieces(db: AsyncSession, order_id: int) -> None:
    """Borra todas las piezas asociadas a la order (sin commit)."""
    await db.execute(
        delete(models.WarehouseOrderPiece).where(models.WarehouseOrderPiece.order_id == order_id)
    )
    await db.flush()