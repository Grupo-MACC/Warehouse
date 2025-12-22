# -*- coding: utf-8 -*-
"""Esquemas Pydantic para Warehouse.

Este módulo define los esquemas Pydantic usados en el microservicio de almacén (warehouse).

- Payload de entrada para simular "order completa" (sin Rabbit aún).
- Payload de entrada para simular "pieza fabricada" (sin Rabbit aún).
- Esquemas de salida básicos para inspeccionar BD más adelante.
"""

from typing import List, Literal, Optional
from datetime import datetime

from pydantic import BaseModel, Field, ConfigDict


PieceType = Literal["A", "B"]
PieceSource = Literal["stock", "manufactured"]


class Message(BaseModel):
    """Esquema genérico para mensajes simples de respuesta."""
    detail: Optional[str] = Field(default=None, example="OK")


class IncomingOrderLine(BaseModel):
    """Línea de entrada de una order: tipo de pieza + cantidad.

    Ejemplos válidos:
    - {"piece_type": "A", "quantity": 5}
    - {"piece_type": "B", "quantity": 3}
    """
    piece_type: PieceType = Field(description="Tipo de pieza (A/B).", example="A")
    quantity: int = Field(description="Cantidad solicitada para ese tipo.", ge=0, example=5)


class IncomingOrder(BaseModel):
    """Order completa que llega a Warehouse (simulación de evento futuro)."""
    order_id: int = Field(description="ID de la order original.", ge=1, example=1001)
    lines: List[IncomingOrderLine] = Field(
        description="Líneas por tipo de pieza. Una order puede incluir A, B o ambas.",
        min_length=1,
        example=[{"piece_type": "A", "quantity": 2}, {"piece_type": "B", "quantity": 1}],
    )


class PieceBuiltEvent(BaseModel):
    """Evento simulado de 'pieza fabricada' que llegaría desde la cola de piezas fabricadas."""
    order_id: int = Field(description="ID de la order a la que pertenece.", ge=1, example=1001)
    piece_type: PieceType = Field(description="Tipo de pieza fabricada (A/B).", example="A")
    manufacturing_date: Optional[datetime] = Field(
        default=None,
        description="Fecha/hora de fabricación. Si no viene, Warehouse puede usar 'now'.",
        example="2025-12-21T12:30:00Z",
    )

class StockAdjustRequest(BaseModel):
    """Payload para ajustar stock sumando/restando unidades.

    Campos:
    - piece_type: Tipo de pieza (A/B).
    - delta: Unidades a sumar (positivo). Si permites negativo, podrías restar stock,
      pero para evitar stock negativo, aquí lo vamos a restringir a >= 1.
    """
    piece_type: PieceType
    delta: int = Field(..., ge=1)


class StockSetRequest(BaseModel):
    """Payload para fijar el stock a un valor exacto.

    Campos:
    - piece_type: Tipo de pieza (A/B).
    - quantity: Cantidad final (>=0).
    """
    piece_type: PieceType
    quantity: int = Field(..., ge=0)


# ---- Salidas básicas (para endpoints) ----------------------------------------

class StockItem(BaseModel):
    """Salida del stock disponible."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    piece_type: PieceType
    quantity: int


class ManufacturingOrderOut(BaseModel):
    """Salida de una order dentro de Warehouse."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    total_a: int
    total_b: int
    to_build_a: int
    to_build_b: int
    finished: bool


class WarehouseOrderPieceOut(BaseModel):
    """Salida de una pieza asociada a una order."""
    model_config = ConfigDict(from_attributes=True)

    id: int
    order_id: int
    piece_type: PieceType
    source: PieceSource
    manufacturing_date: Optional[datetime]
