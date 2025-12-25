# Machine (microservicio de fabricación)

Este microservicio representa una **máquina** capaz de fabricar **piezas tipo A o tipo B**. Está pensado para funcionar en un entorno de microservicios con **RabbitMQ** (mensajería/eventos), **Consul** (service discovery/registro) y un servicio **Auth** que publica su **clave pública** para validar JWT.

> Idea clave: **Machine NO decide qué fabricar**. Fabrica exactamente lo que **Warehouse** le manda, **1 pieza por mensaje**.

---

## Qué hace este microservicio

1. **Se registra en Consul** al arrancar (para que otros servicios lo descubran).
2. **Consume eventos de fabricación** desde RabbitMQ:
   - Entrada: mensajes con **una pieza** en routing key `machine.a` o `machine.b`.
   - La máquina valida que el `piece_type` recibido coincide con su tipo configurado.
3. **Simula la fabricación** (con un `sleep` aleatorio configurable).
4. **Publica el evento de pieza terminada** en RabbitMQ:
   - Salida: routing key `piece.done` con `{order_id, piece_id, piece_type, manufacturing_date}`.
5. **Escucha eventos de Auth** (`auth.running` / `auth.not_running`):
   - Cuando recibe `auth.running`, **descubre Auth por Consul**, descarga `/auth/public-key` y la guarda en disco.
   - Esto habilita la validación de JWT de los endpoints protegidos.

---

## Flujo de trabajo (end-to-end)

### 1) Fabricación de piezas (Warehouse → Machine → Warehouse)

```
Warehouse
  └─ publica 1 pieza por mensaje
     ├─ routing key: machine.a  (piezas A)
     └─ routing key: machine.b  (piezas B)
         ↓
RabbitMQ (exchange topic)
         ↓
cola durable por tipo (compartida entre réplicas del mismo tipo)
  ├─ machine_a_queue
  └─ machine_b_queue
         ↓
Machine (consume 1 mensaje, fabrica, ACK al final)
         ↓
publica evento: piece.done
         ↓
Warehouse (consume piece.done y actualiza su estado)
```

**Reparto justo entre réplicas del mismo tipo**
- El consumer configura `prefetch_count=1`.
- El ACK se hace **al final**, tras fabricar.
- Resultado: cada instancia procesa **una pieza cada vez**, y RabbitMQ reparte equitativamente.

> Semántica real: **at-least-once**.
> - Si la máquina se cae *después* de publicar `piece.done` pero *antes* del ACK, RabbitMQ reentregará el mensaje y la pieza podría “fabricarse” dos veces.
> - Lo correcto es que el consumidor de `piece.done` (normalmente Warehouse) sea **idempotente** (deduplicación por `piece_id`).

### 2) Disponibilidad de Auth (Auth → Machine)

```
Auth publica:
  - auth.running / auth.not_running
         ↓
Machine escucha esos eventos
         ↓
Si status == running:
  - Descubre Auth en Consul
  - GET {auth_url}/auth/public-key
  - Guarda la clave en PUBLIC_KEY_PATH
```

Esto es importante porque:
- `/machine/health` devuelve **503** si no existe clave pública.
- `/machine/status` está protegido por JWT (`get_current_user`).

---

## Contratos de eventos (RabbitMQ)

### Entrada: pieza a fabricar

Routing keys:
- `machine.a`
- `machine.b`

Body JSON esperado (1 pieza por mensaje):

```json
{
  "piece_id": "uuid-o-string",
  "order_id": 123,
  "piece_type": "A",
  "order_date": "2025-12-25T10:15:30Z"
}
```

Notas:
- `order_date` hoy **no se usa** en la lógica de fabricación (se acepta para futuro logging/BD).
- Si llega un `piece_type` distinto al configurado para la máquina, el mensaje se **ACKea y se descarta** (para no generar bucles por routing mal configurado).

### Salida: pieza terminada

Routing key:
- `piece.done`

Body JSON publicado:

```json
{
  "order_id": 123,
  "piece_id": "uuid-o-string",
  "piece_type": "A",
  "manufacturing_date": "2025-12-25T10:16:02.123456Z"
}
```

---

## API HTTP (FastAPI)

Base path: `/machine`

### `GET /machine/health`
Health check lógico del servicio.

- ✅ `200` si la clave pública de Auth está disponible (ver `check_public_key()`).
- ❌ `503` si falta la clave pública (típico al arrancar antes de que Auth publique `auth.running`).

Respuesta:

```json
{"detail": "OK"}
```

### `GET /machine/status`
Devuelve el estado actual de la máquina (`Waiting` o `Working`).

- Requiere JWT válido (`get_current_user`).
- Respuesta típica: `"Waiting"`.

---

## Configuración por variables de entorno

### Machine (fabricación)
- `MACHINE_PIECE_TYPE` (por defecto `A`): tipo de pieza que fabrica esta instancia (`A` o `B`).
- `MACHINE_ROUTING_KEY` (opcional): por defecto `machine.{a|b}`.
- `MACHINE_QUEUE_NAME` (opcional): por defecto `machine_{a|b}_queue`.
- `MACHINE_MIN_SECONDS` (por defecto `5`): mínimo de segundos de fabricación (simulación).
- `MACHINE_MAX_SECONDS` (por defecto `20`): máximo de segundos de fabricación (simulación).

### Consul (registro/descubrimiento)
- `CONSUL_HOST` (Dockerfile: `consul`)
- `CONSUL_PORT` (Dockerfile: `8500`)
- `SERVICE_NAME` (Dockerfile: `machine`)
- `SERVICE_PORT` (Dockerfile: `5001`)
- `SERVICE_ID` (Dockerfile: `machine-1`)

### RabbitMQ (vía microservice-chassis)
El servicio usa `microservice-chassis-grupo2` para crear canal/exchange. En Dockerfile se exportan:
- `RABBITMQ_HOST` (Dockerfile: `rabbitmq`)
- `RABBITMQ_USER` (Dockerfile: `guest`)
- `RABBITMQ_PASSWORD` (Dockerfile: `guest`)

### Auth / clave pública
- `PUBLIC_KEY_PATH` (Dockerfile: `/home/pyuser/code/auth_public.pem`)

---

## Ejecutar en local (sin Docker)

Requisitos:
- Python 3.12
- RabbitMQ accesible
- Consul accesible (si no, el registro en Consul fallará)
- Auth (o, como mínimo, un fichero de clave pública en `PUBLIC_KEY_PATH`)

Instalación:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Ejecución:

```bash
export SERVICE_NAME=machine
export SERVICE_PORT=5001
export SERVICE_ID=machine-1
export CONSUL_HOST=localhost
export CONSUL_PORT=8500

export RABBITMQ_HOST=localhost
export RABBITMQ_USER=guest
export RABBITMQ_PASSWORD=guest

export MACHINE_PIECE_TYPE=A
export MACHINE_MIN_SECONDS=2
export MACHINE_MAX_SECONDS=5

uvicorn app_machine.main:app --host 0.0.0.0 --port 5001 --reload
```

---

## Ejecutar con Docker

Build:

```bash
docker build -t machine:local .
```

Run (ejemplo básico; normalmente irá en docker-compose junto a rabbitmq/consul/auth):

```bash
docker run --rm -p 5001:5001   -e MACHINE_PIECE_TYPE=A   -e SERVICE_ID=machine-a-1   --name machine-a-1   machine:local
```

Para levantar una máquina tipo B:

```bash
docker run --rm -p 5002:5001   -e MACHINE_PIECE_TYPE=B   -e SERVICE_ID=machine-b-1   --name machine-b-1   machine:local
```

> Fíjate que internamente el contenedor escucha en 5001, pero puedes mapearlo a otro puerto en el host.

---

## Estructura del repositorio

```
app_machine/
  main.py                         # FastAPI + lifespan (Consul + consumers RabbitMQ)
  routers/
    machine_router.py             # Endpoints /machine/health y /machine/status
  business_logic/
    async_machine.py              # Simulación de fabricación (sleep aleatorio)
  broker/
    machine_broker_service.py     # Consumers RabbitMQ + publish piece.done + logs
    setup_rabbitmq.py             # (legacy) setup antiguo de exchange/queue
  services/
    machine_service.py            # (legacy) add_pieces_to_queue consultando Order por HTTP
  sql/
    models.py, schemas.py         # (legacy/todo) modelos/schemas no cableados hoy
Dockerfile
entrypoint.sh
```

---

## Notas profesionales (lo bueno y lo mejorable)

✅ **Bien planteado**
- Modelo de consumo: `prefetch_count=1` + ACK al final → buen reparto y “una pieza a la vez”.
- Separación: broker (RabbitMQ) vs lógica de fabricación (Machine).
- Integración con Auth por evento (`auth.running`) evita acoplar “polling” constante.

⚠️ **Puntos a vigilar**
- **At-least-once**: posible duplicación si se cae tras publicar `piece.done` y antes del ACK.
- La cola de Auth está hardcodeada como `machine_queue` (si escalas, varias instancias consumen los mismos eventos; normalmente OK, pero es un diseño consciente).
- `setup_rabbitmq.py` y `services/machine_service.py` parecen **código heredado** del enfoque anterior (HTTP/colas internas) y hoy no forman parte del flujo principal.
- Hay modelos SQL (`sql/models.py`) pero **no hay inicialización de BD** ni escritura/lectura en runtime todavía.

---

## Roadmap sugerido (lo siguiente que tiene sentido implementar)

1. **Persistencia por máquina (SQLite/Postgres)**
   - Registrar cada pieza fabricada: `order_id`, `piece_id`, `order_date`, `manufacturing_date`.
2. **Reanudación tras apagado**
   - Persistir la `working_piece` y reintentar al arrancar.
3. **Blacklist por order_id**
   - Si Warehouse cancela una orden, Machine debe poder **ignorar** piezas de esa orden sin bloquear el consumo de la cola (mejor con dedupe/estado compartido o evento de cancelación).

---

## Licencia

MIT (ver metadatos de OpenAPI).
