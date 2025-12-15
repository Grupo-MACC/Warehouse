# -*- coding: utf-8 -*-
"""Esquemas Pydantic para el microservicio de almacén."""

from typing import Optional

from pydantic import BaseModel, Field, ConfigDict


class Message(BaseModel):
    """Esquema genérico para mensajes simples de respuesta."""

    detail: Optional[str] = Field(
        default=None,
        example="Mensaje de éxito o error",
        description="Descripción corta del resultado de la operación.",
    )


class WarehouseItemBase(BaseModel):
    """Campos comunes de una entrada de almacén."""

    process_id: int = Field(
        description="Identificador del proceso de fabricación del que provienen las piezas.",
        example=42,
    )
    piece_type: str = Field(
        description="Tipo de pieza. Por ejemplo, 'A' o 'B'.",
        example="A",
        min_length=1,
        max_length=32,
    )
    quantity: int = Field(
        description="Cantidad de piezas almacenadas.",
        example=10,
        ge=0,
    )


class WarehouseItemCreate(WarehouseItemBase):
    """Esquema para crear una nueva entrada de almacén."""


class WarehouseItem(WarehouseItemBase):
    """Esquema de salida de una entrada de almacén."""

    model_config = ConfigDict(from_attributes=True)

    id: int = Field(
        description="Identificador interno de la entrada de almacén.",
        example=1,
    )
