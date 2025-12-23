# Warehouse

Microservicio **Warehouse** (almacén/producción) del proyecto.

## Paso 0 (estado actual)

Este repositorio está en el **Paso 0** del plan de implementación:

- El servicio arranca de forma fiable en Docker.
- Expone un endpoint de healthcheck:
  - `GET /warehouse/health` → `{"detail": "OK"}`

En este paso **no** se inicializa Consul ni RabbitMQ, y **no** se exige
autenticación. La prioridad es validar el arranque y la estructura del
proyecto para poder iterar con commits pequeños.

## Ejecución local (sin Docker)

```bash
pip install -r requirements.txt
export SERVICE_PORT=5005
uvicorn app_warehouse.main:app --reload --port 5005
```

## Ejecución con Docker

```bash
docker build -t warehouse .
docker run --rm -p 5005:5005 warehouse
```

Luego:

```bash
curl http://localhost:5005/warehouse/health
```
