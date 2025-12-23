# -*- coding: utf-8 -*-
"""Modelos de base de datos para el microservicio Warehouse.

Este módulo define los modelos SQLAlchemy para las tablas específicas
del microservicio de almacén (warehouse).
- warehouse_stock: inventario *disponible* (reutilizable) por tipo A/B.
- warehouse_manufacturing_order: orders "en fabricación" (lo que llega a Warehouse).
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


class WarehouseManufacturingOrder(BaseModel):
    """Orders en fabricación dentro de Warehouse.

    Campos clave:
    - id: usamos el id de Order como PK para evitar mapeos raros (fiel al estilo del resto).
    - total_a/total_b: cantidades totales pedidas por tipo.
    - finished: marcador simple (de momento).
      Más adelante podrás evolucionarlo a status (Manufacturing/Finished/Cancelled).
    """

    __tablename__ = "warehouse_manufacturing_order"

    id = Column(Integer, primary_key=True)  # id de la order original
    total_a = Column(Integer, nullable=False, default=0)
    total_b = Column(Integer, nullable=False, default=0)

    # Lo que realmente hay que fabricar tras descontar stock (útil para publicar a Rabbit más tarde)
    to_build_a = Column(Integer, nullable=False, default=0)
    to_build_b = Column(Integer, nullable=False, default=0)

    finished = Column(Boolean, nullable=False, default=False)

    pieces = relationship(
        "WarehouseOrderPiece",
        back_populates="order",
        lazy="joined",
        cascade="all, delete-orphan",
    )


class WarehouseOrderPiece(BaseModel):
    """Piezas asociadas a una order.

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
        ForeignKey("warehouse_manufacturing_order.id", ondelete="cascade"),
        nullable=False,
        index=True,
    )

    order = relationship("WarehouseManufacturingOrder", back_populates="pieces", lazy="joined")
