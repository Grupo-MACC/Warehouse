#!/bin/sh

echo "Service: ${SERVICE_NAME}"
IP=$(hostname -i)
export IP
echo "IP: ${IP}"

terminate() {
  echo "Termination signal received, shutting down..."
  kill -SIGTERM "$UVICORN_PID"
  wait "$UVICORN_PID"
  echo "Uvicorn has been terminated"
}

trap terminate SIGTERM SIGINT

echo "Starting Uvicorn (HTTP interno)..."

uvicorn app_warehouse.main:app \
  --host 0.0.0.0 \
  --port 5005 &

UVICORN_PID=$!

wait "$UVICORN_PID"