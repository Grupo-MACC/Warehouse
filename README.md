# Warehouse (microservicio de almacén y orquestación de fabricación)

Este microservicio representa el **almacén (Warehouse)**. Su responsabilidad es **gestionar stock** y **coordinar la fabricación** de piezas cuando una order no puede cubrirse completamente con inventario.

> Idea clave: **Warehouse decide qué se fabrica y qué no**, basándose en stock y en la order completa que recibe.

---

## Qué hace este microservicio

1. **Se registra en Consul** al arrancar (para service discovery).
2. **Mantiene su propia base de datos** (SQLite por defecto) con:
   - stock disponible por tipo de pieza (`A`/`B`)
   - orders en fabricación
   - piezas asociadas a cada order (tanto “desde stock” como “fabricadas”)
3. **Consume orders entrantes por RabbitMQ** y:
   - calcula totales de piezas `A`/`B`
   - consume stock disponible para evitar fabricar lo que ya existe
   - persiste la order y las piezas cubiertas por stock
   - **publica 1 mensaje por pieza** a las colas de `Machine` (`machine.a` / `machine.b`)
4. **Consume eventos de piezas fabricadas** (`piece.done`) y:
   - registra la pieza en BD
   - recalcula si la order está completada
   - marca `finished=True` cuando el número de piezas registradas alcanza los totales
5. **Consume eventos de cancelación** (`process.canceled`) **solo para log** (la lógica de compensación está marcada como TODO).
6. Expone endpoints HTTP **de debug/simulación** para probar la lógica sin RabbitMQ.

---

## Flujo de trabajo (end-to-end)

### 1) Entrada de una order (Order → Warehouse)

**Modo real (event-driven):**
- Warehouse consume mensajes de RabbitMQ desde `warehouse_order_queue`.
- Por defecto escucha routing keys: `warehouse.order` y `order.confirmed` (configurable).

**Modo debug (HTTP):**
- Puedes simular la llegada de una order con `POST /warehouse/orders`.

**Qué hace Warehouse al recibir la order:**

- **Idempotencia básica**: si `order_id` ya existe en BD (en fabricación), se devuelve la existente y no se replantea.
- Suma totales `total_a` / `total_b` a partir de `lines`.
- Consume stock disponible y lo descuenta (`consume_stock`).
- Calcula `to_build_a` / `to_build_b` (lo que hay que fabricar).
- Crea la order en fabricación.
- Inserta en `warehouse_order_piece` las piezas cubiertas desde stock (`source="stock"`).
- Construye la lista de piezas a fabricar (una entrada por pieza).

> Importante: el estado final `finished` se decide por **conteo real de piezas registradas** vs totales, no por `to_build_*`. Esto hace el sistema más robusto cuando llegan piezas “por detrás”.

---

### 2) Planificación de fabricación (Warehouse → Machine)

Si hay piezas a fabricar, Warehouse publica **1 mensaje por pieza** hacia las máquinas:

- `machine.a` para piezas tipo `A`
- `machine.b` para piezas tipo `B`

Cada mensaje incluye:
- `piece_id` (UUID generado por Warehouse)
- `order_id`
- `piece_type`
- `order_date` (ISO)

---

### 3) Pieza fabricada (Machine → Warehouse)

Las máquinas publican `piece.done` cuando terminan una pieza.

Warehouse consume esos eventos desde `warehouse_built_queue` (por defecto bind a `piece.done`) y:

- valida el payload
- abre transacción en BD
- registra la pieza como `source="fabricated"`
- recalcula si la order está completa
- hace commit

**Requeue en condición de carrera**
- Si llega una pieza antes de que exista la order en BD, se lanza `ValueError("no existe")` y el mensaje se **reencola** (`requeue=True`).

---

### 4) Cancelación de order (Order → Warehouse)

Warehouse consume `process.canceled` desde `process_canceled_queue`.

Actualmente:
- **solo loguea**
- deja un TODO para revertir stock, limpiar DB, etc.

---

## Base de datos

Por defecto:
- `SQLALCHEMY_DATABASE_URL=sqlite+aiosqlite:///./warehouse.db`

Tablas principales:

### `warehouse_stock`
Stock reutilizable disponible.
- `piece_type` (`A`/`B`, único)
- `quantity` (>=0)

### `warehouse_fabrication_order`
Orders “en fabricación” (las que llegan a Warehouse).
- `id` (usa el `order_id` original)
- `total_a`, `total_b` (demanda total)
- `to_build_a`, `to_build_b` (lo que faltaba tras consumir stock)
- `finished` (bool)

### `warehouse_order_piece`
Una fila por pieza asociada a la order.
- `order_id` (FK → fabrication_order)
- `piece_type` (`A`/`B`)
- `source` (`stock` / `fabricated`)
- `fabrication_date` (solo para piezas fabricadas)

> La completitud de una order se recalcula como:  
> `COUNT(piezas A) >= total_a` y `COUNT(piezas B) >= total_b`.

---

## Configuración por variables de entorno

### Servicio / Consul
- `CONSUL_HOST` (Dockerfile: `consul`)
- `CONSUL_PORT` (Dockerfile: `8500`)
- `SERVICE_NAME` (Dockerfile: `warehouse`)
- `SERVICE_PORT` (Dockerfile: `5005`)
- `SERVICE_ID` (Dockerfile: `warehouse-1`)
- `APP_VERSION` (default `1.0.0`)

### BD
- `SQLALCHEMY_DATABASE_URL` (default `sqlite+aiosqlite:///./warehouse.db`)

### RabbitMQ (vía microservice-chassis)
- `RABBITMQ_HOST` (Dockerfile: `rabbitmq`)
- `RABBITMQ_USER` / `RABBITMQ_PASSWORD`

### Colas / routing keys
- `WAREHOUSE_ORDER_QUEUE` (default `warehouse_order_queue`)
- `WAREHOUSE_ORDER_ROUTING_KEYS` (default `warehouse.order,order.confirmed`)
- `MACHINE_A_ROUTING_KEY` (default `machine.a`)
- `MACHINE_B_ROUTING_KEY` (default `machine.b`)
- `WAREHOUSE_BUILT_QUEUE` (default `warehouse_built_queue`)
- `WAREHOUSE_BUILT_ROUTING_KEYS` (default `piece.done`)

**Opcionales (si usas `setup_rabbitmq.py`):**
- `MACHINE_A_QUEUE` (default `machine_a_queue`)
- `MACHINE_B_QUEUE` (default `machine_b_queue`)

### Auth (clave pública)
- `PUBLIC_KEY_PATH` (Dockerfile: `/home/pyuser/code/auth_public.pem`)

---

## Ejecutar en local (sin Docker)

Requisitos:
- Python 3.12
- (si usas Rabbit) RabbitMQ accesible + Consul accesible
- una clave pública disponible en `PUBLIC_KEY_PATH` (o `check_public_key()` devolverá 503)

Instalación:
```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Arranque:
```bash
export SQLALCHEMY_DATABASE_URL="sqlite+aiosqlite:///./warehouse.db"
uvicorn app_warehouse.main:app --host 0.0.0.0 --port 5005 --reload
```

---

## Ejecutar con Docker

Build:
```bash
docker build -t warehouse:local .
```

Run (mínimo):
```bash
docker run --rm -p 5005:5005 --name warehouse warehouse:local
```

En despliegue real normalmente irá en `docker-compose` junto a `rabbitmq`, `consul`, `auth`, `order`, `machine`.

---

## Estructura del repositorio

```
app_warehouse/
  main.py                          # FastAPI + lifespan (Consul + consumers RabbitMQ)
  routers/
    warehouse_router.py            # Endpoints debug / simulación
  services/
    warehouse_service.py           # Lógica de negocio: stock, orders, piezas, finished
  broker/
    warehouse_broker_service.py    # Consumers/publishers RabbitMQ
    setup_rabbitmq.py              # Declaración colas/bindings (no se llama desde main)
  sql/
    database.py                    # SQLAlchemy async + SessionLocal
    models.py                      # Tablas (stock / fabrication_order / order_piece)
    schemas.py                     # Pydantic schemas
    crud.py                        # Acceso BD (helpers)
  consul_client.py                 # Registro/descubrimiento Consul
```

## Licencia
MIT (ver metadatos de FastAPI/OpenAPI).
