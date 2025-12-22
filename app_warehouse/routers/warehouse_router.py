# -*- coding: utf-8 -*-
"""Definición de rutas del microservicio Warehouse.

En esta iteración NO usamos RabbitMQ real.
Estos endpoints simulan:
- la llegada de una order completa (que más adelante llegará por suscripción Rabbit)
- la llegada de piezas fabricadas (que más adelante llegará por suscripción Rabbit)

El router debe mantenerse fino:
- validar entrada
- llamar a la lógica en services/warehouse_service.py
- traducir errores a HTTP
"""

import logging
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from microservice_chassis_grupo2.core.dependencies import get_db, check_public_key

from sql import crud, schemas
from services import warehouse_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/warehouse")


@router.get(
    "/health",
    summary="Health check endpoint",
    response_model=schemas.Message,
)
async def health_check():
    """Healthcheck básico (fiel al estilo de los otros microservicios).

    Nota:
    - Si no existe la clave pública, algunos microservicios devuelven 503.
    - Esto no afecta a la lógica interna, pero mantiene el patrón del ecosistema.
    """
    logger.debug("GET '/warehouse/health' endpoint called.")
    if check_public_key():
        return {"detail": "OK"}
    raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail="Service not available")

#region /orders
@router.post(
    "/orders",
    summary="Simula llegada de una order completa a Warehouse",
    status_code=status.HTTP_200_OK,
)
async def ingest_order(
    order_in: schemas.IncomingOrder,
    db: AsyncSession = Depends(get_db),
):
    """Simula la entrada de una order completa.

    Flujo:
    - Warehouse descuenta stock disponible (A/B) para no fabricar lo que ya existe.
    - Crea la order en fabricación.
    - Registra como piezas de la order las piezas que han salido de stock (source='stock').
    - Devuelve:
      - la order creada (o existente si ya estaba)
      - la lista de "piezas a fabricar" (una entrada por pieza), para que puedas
        ver lo que luego se publicaría en colas A/B.
    """
    try:
        db_order, pieces_to_build = await warehouse_service.recibir_order_completa(db, order_in)
        return {
            "order": {
                "id": db_order.id,
                "total_a": db_order.total_a,
                "total_b": db_order.total_b,
                "to_build_a": db_order.to_build_a,
                "to_build_b": db_order.to_build_b,
                "finished": db_order.finished,
            },
            "pieces_to_build": [
                {
                    "order_id": m["order_id"],
                    "piece_type": m["piece_type"],
                    # datetime es serializable por FastAPI (ISO8601)
                    "date": m["date"],
                }
                for m in pieces_to_build
            ],
        }

    except ValueError as exc:
        # Errores de validación/negocio: 400
        logger.error("[WAREHOUSE] Error procesando order: %s", exc)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from exc
    except Exception as exc:
        # Errores inesperados: 500
        logger.error("[WAREHOUSE] Error inesperado en ingest_order: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error") from exc

#region /pieces/built
@router.post(
    "/pieces/built",
    summary="Simula llegada de una pieza fabricada a Warehouse",
    status_code=status.HTTP_200_OK,
)
async def piece_built(
    event: schemas.PieceBuiltEvent,
    db: AsyncSession = Depends(get_db),
):
    """Simula la llegada de una pieza fabricada.

    Flujo:
    - Inserta la pieza como source='manufactured'
    - Recalcula finished comparando lo registrado vs lo total esperado
    """
    try:
        db_order = await warehouse_service.recibir_pieza_fabricada(db, event)
        return {
            "order": {
                "id": db_order.id,
                "total_a": db_order.total_a,
                "total_b": db_order.total_b,
                "to_build_a": db_order.to_build_a,
                "to_build_b": db_order.to_build_b,
                "finished": db_order.finished,
            }
        }

    except ValueError as exc:
        # Si la order no existe, es 404. Si no, 400.
        msg = str(exc)
        logger.error("[WAREHOUSE] Error registrando pieza: %s", msg)
        if "no existe" in msg.lower():
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=msg) from exc
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=msg) from exc
    except Exception as exc:
        logger.error("[WAREHOUSE] Error inesperado en piece_built: %s", exc)
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Internal server error") from exc

#region /orders/{order_id}
@router.get(
    "/orders/{order_id}",
    summary="Inspecciona el estado de una order en fabricación (debug)",
    status_code=status.HTTP_200_OK,
)
async def get_order_status(
    order_id: int,
    db: AsyncSession = Depends(get_db),
):
    """Endpoint de depuración para ver si la order está completa y cuántas piezas hay registradas.

    Esto es MUY útil ahora porque todavía no hay Rabbit:
    - Puedes ver si el conteo cuadra
    - Puedes ver si finished cambia cuando insertas piezas
    """
    db_order = await crud.get_manufacturing_order(db, order_id)
    if db_order is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=f"Order {order_id} no existe.")

    count_a = await crud.count_order_pieces_by_type(db, order_id=order_id, piece_type="A")
    count_b = await crud.count_order_pieces_by_type(db, order_id=order_id, piece_type="B")

    return {
        "order": {
            "id": db_order.id,
            "total_a": db_order.total_a,
            "total_b": db_order.total_b,
            "to_build_a": db_order.to_build_a,
            "to_build_b": db_order.to_build_b,
            "finished": db_order.finished,
        },
        "pieces_registered": {"A": count_a, "B": count_b},
    }
