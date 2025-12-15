# -*- coding: utf-8 -*-
"""Modelos de base de datos para el microservicio de almacén."""

from sqlalchemy import Column, Integer, String
from microservice_chassis_grupo2.sql.models import BaseModel


class WarehouseItem(BaseModel):
    """Tabla que representa piezas almacenadas tras cancelar procesos de fabricación.

    Esta tabla guarda el inventario *agregado* de piezas por tipo y proceso.
    Más adelante podrás refinar el modelo (añadir reservas, lotes, etc.).
    """

    __tablename__ = "warehouse_items"

    id = Column(Integer, primary_key=True, index=True)
    process_id = Column(Integer, nullable=False, index=True)
    piece_type = Column(String(32), nullable=False, index=True)
    quantity = Column(Integer, nullable=False, default=0)
