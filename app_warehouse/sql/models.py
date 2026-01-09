# -*- coding: utf-8 -*-
"""Modelos de base de datos para el microservicio Warehouse.

Este módulo define los modelos SQLAlchemy para las tablas específicas
del microservicio de almacén (warehouse).
- warehouse_stock: inventario *disponible* (reutilizable) por tipo A/B.
- warehouse_order: orders "en fabricación" (lo que llega a Warehouse).
- warehouse_order_piece: piezas asociadas a una order.
  Aquí se guardan:
  - piezas que se asignaron desde stock (source='stock')
  - piezas que llegan fabricadas (source='manufactured')

Nota: todos los modelos heredan de BaseModel del chassis, que aporta
campos comunes como creation_date, update_date, etc.
"""

from sqlalchemy import Column, Integer, String, Boolean, DateTime, ForeignKey
from sqlalchemy.orm import relationship

from microservice_chassis_grupo2.sql.models import BaseModel


# ---------------------- GLOBAL Status ----------------------
WAREHOUSE_ORDER_STATUS_IN_MANUFACTURING = "IN_MANUFACTURING"
WAREHOUSE_ORDER_STATUS_CANCELING = "CANCELING"
WAREHOUSE_ORDER_STATUS_CANCELED = "CANCELED"
WAREHOUSE_ORDER_STATUS_COMPLETED = "COMPLETED"

class WarehouseStock(BaseModel):
    """Stock disponible reutilizable por tipo de pieza.

    Nota:
    - Este stock representa *disponible para nuevas orders*.
    - Cuando una pieza se usa para cubrir una order entrante, aquí se descuenta.
    - Cuando una order se cancela (SAGA futura), aquí se incrementará.
    """

    __tablename__ = "warehouse_stock"

    id = Column(Integer, primary_key=True)
    piece_type = Column(String(1), nullable=False, unique=True, index=True)  # 'A' o 'B'
    quantity = Column(Integer, nullable=False, default=0)


class WarehouseOrder(BaseModel):
    """Order en Warehouse (única fuente de verdad).

    status:
        - IN_MANUFACTURING: activa
        - CANCELING: se solicitó cancelación (esperando confirmación)
        - CANCELED: cancelada (piezas devueltas a stock)
        - COMPLETED: completada (fabricación finalizada)
    """
    __tablename__ = "warehouse_order"

    id = Column(Integer, primary_key=True)  # id de la order original
    total_a = Column(Integer, nullable=False, default=0)
    total_b = Column(Integer, nullable=False, default=0)

    to_build_a = Column(Integer, nullable=False, default=0)
    to_build_b = Column(Integer, nullable=False, default=0)

    status = Column(String(32), nullable=False, default=WAREHOUSE_ORDER_STATUS_IN_MANUFACTURING, index=True)

    # Para el SAGA de cancelación (correlación con Order)
    cancel_saga_id = Column(String(64), nullable=True)

    canceled_at = Column(DateTime(timezone=True), nullable=True, server_default=None)
    completed_at = Column(DateTime(timezone=True), nullable=True, server_default=None)

    pieces = relationship(
        "WarehouseOrderPiece",
        back_populates="order",
        lazy="joined",
        cascade="all, delete-orphan",
    )


class WarehouseOrderPiece(BaseModel):
    """Piezas asociadas a una order y que ya han sido manufacturadas.

    source:
    - 'stock'        -> pieza aportada desde inventario disponible (ya existía)
    - 'manufactured' -> pieza llegada desde máquina / fabricación

    manufacturing_date:
    - Si source='manufactured', debería venir del evento.
    - Si source='stock', normalmente no conoces cuándo se fabricó originalmente.
      Puedes dejarlo a NULL y usar creation_date (del BaseModel) como "fecha de asignación".
    """

    __tablename__ = "warehouse_order_piece"

    id = Column(Integer, primary_key=True)
    piece_type = Column(String(1), nullable=False, index=True)  # 'A' o 'B'
    source = Column(String(32), nullable=False, default="manufactured")
    manufacturing_date = Column(DateTime(timezone=True), nullable=True, server_default=None)

    order_id = Column(
        Integer,
        ForeignKey("warehouse_order.id", ondelete="cascade"),
        nullable=False,
        index=True,
    )

    order = relationship("WarehouseOrder", back_populates="pieces", lazy="joined")

class WarehouseManufacturingCancellation(BaseModel):
    """
    Guarda estado de cancelación (CANCEL_REQUESTED → CONFIRMED).

    Campos:
        - order_id: pedido afectado
        - saga_id: correlación del SAGA
        - machine_a: confirmación máquina A
        - machine_b: confirmación máquina B
    """
    __tablename__ = "warehouse_manufacturing_cancellation"

    order_id = Column(Integer, primary_key=True)
    saga_id = Column(String(64), nullable=False)

    machine_a = Column(Boolean, default=False)
    machine_b = Column(Boolean, default=False)
