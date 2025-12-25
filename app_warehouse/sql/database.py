# -*- coding: utf-8 -*-
"""Configuración de SQLAlchemy Async para Warehouse.

Nota:
    - expire_on_commit=False evita que SQLAlchemy "expire" (invalide) atributos de objetos ORM
      tras hacer commit. Esto es muy recomendable en microservicios async porque acceder a
      atributos expirados provoca recargas implícitas (IO) que en async puede disparar
      MissingGreenlet.
"""

import os
from sqlalchemy.orm import sessionmaker, declarative_base
from sqlalchemy.ext.asyncio import create_async_engine, AsyncSession

SQLALCHEMY_DATABASE_URL = os.getenv(
    "SQLALCHEMY_DATABASE_URL",
    "sqlite+aiosqlite:///./warehouse.db",
)

engine = create_async_engine(
    SQLALCHEMY_DATABASE_URL,
    connect_args={"check_same_thread": False},
    echo=False,
)

SessionLocal = sessionmaker(
    autocommit=False,
    autoflush=False,
    bind=engine,
    class_=AsyncSession,
    future=True,
    expire_on_commit=False,  # ✅ CLAVE
)

Base = declarative_base()
