#!/usr/bin/env bash
set -euo pipefail

# -----------------------------------------------------------------------------
# Entrypoint genérico para microservicios FastAPI + Uvicorn (HTTPS only)
#
# Objetivo:
#   - Arrancar siempre con TLS (ssl-certfile / ssl-keyfile).
#   - Auto-detectar el módulo Uvicorn (por defecto "main:app") de forma segura.
#   - Auto-detectar certificado/clave dentro de /certs si no se pasan por env.
#
# Variables útiles (opcionales):
#   - APP_MODULE:          Ej: "main:app" o "app_payment.main:app"
#   - SERVICE_PORT:        Puerto donde escucha el micro
#   - UVICORN_HOST:        Por defecto 0.0.0.0
#   - UVICORN_RELOAD:      "1" para dev
#   - SERVICE_CERT_FILE:   Ruta explícita al cert (recomendado)
#   - SERVICE_KEY_FILE:    Ruta explícita a la key (recomendado)
#   - UVICORN_EXTRA_ARGS:  Args extra (ej: "--log-level debug")
# -----------------------------------------------------------------------------

echo "Service: ${SERVICE_NAME:-unknown}"
IP="$(hostname -i 2>/dev/null || true)"
export IP
echo "IP: ${IP}"

HOST="${UVICORN_HOST:-0.0.0.0}"
PORT="${SERVICE_PORT:-5000}"
RELOAD="${UVICORN_RELOAD:-0}"
EXTRA_ARGS="${UVICORN_EXTRA_ARGS:-}"

# -----------------------------------------------------------------------------
# Helpers
# -----------------------------------------------------------------------------
_die() {
  echo "ERROR: $*" >&2
  exit 1
}

# Devuelve 0 si python puede importar <module> y tiene atributo <attr>.
_py_has_attr() {
  local module="$1"
  local attr="$2"
  python - <<PY >/dev/null 2>&1
import importlib
m = importlib.import_module("${module}")
getattr(m, "${attr}")
PY
}

# Busca un único fichero en /certs (recursivo). Si hay 0 o >1, falla.
_find_single_cert_file() {
  local pattern="$1"
  local desc="$2"

  # Nota: en tus contenedores normalmente SOLO montas el cert/key del propio servicio + ca.pem,
  # así que lo habitual es que exista exactamente 1 match.
  mapfile -t matches < <(find /certs -type f -name "${pattern}" 2>/dev/null | sort || true)

  if [[ "${#matches[@]}" -eq 0 ]]; then
    _die "No encuentro ${desc} en /certs (pattern=${pattern}). Define SERVICE_CERT_FILE/SERVICE_KEY_FILE o revisa volúmenes."
  fi

  if [[ "${#matches[@]}" -gt 1 ]]; then
    echo "Encontré múltiples candidatos para ${desc}:" >&2
    printf ' - %s\n' "${matches[@]}" >&2
    _die "Ambiguo. Define explícitamente SERVICE_CERT_FILE/SERVICE_KEY_FILE."
  fi

  echo "${matches[0]}"
}

# -----------------------------------------------------------------------------
# 1) Determinar APP_MODULE (módulo Uvicorn)
# -----------------------------------------------------------------------------
APP_MODULE="${APP_MODULE:-}"

if [[ -z "${APP_MODULE}" ]]; then
  # Caso típico en tus micros: PYTHONPATH apunta al directorio del micro y existe main.py con app=FastAPI(...)
  if _py_has_attr "main" "app"; then
    APP_MODULE="main:app"
  elif _py_has_attr "app" "app"; then
    APP_MODULE="app:app"
  else
    # Fallback: buscar main.py bajo /home/pyuser/code (máx profundidad 2)
    mapfile -t mains < <(find /home/pyuser/code -maxdepth 2 -type f -name "main.py" 2>/dev/null | sort || true)

    if [[ "${#mains[@]}" -eq 1 ]]; then
      # Si es /home/pyuser/code/main.py -> main:app
      if [[ "${mains[0]}" == "/home/pyuser/code/main.py" ]]; then
        APP_MODULE="main:app"
      else
        # Si es /home/pyuser/code/app_x/main.py -> app_x.main:app
        pkg="$(basename "$(dirname "${mains[0]}")")"
        APP_MODULE="${pkg}.main:app"
      fi
    else
      echo "No pude auto-detectar APP_MODULE." >&2
      echo "Sugerencia: exporta APP_MODULE explícito, ej: APP_MODULE=main:app" >&2
      _die "No se puede arrancar Uvicorn sin APP_MODULE."
    fi
  fi
fi

# -----------------------------------------------------------------------------
# 2) Determinar CERT_FILE / KEY_FILE (TLS)
# -----------------------------------------------------------------------------
CERT_FILE="${SERVICE_CERT_FILE:-}"
KEY_FILE="${SERVICE_KEY_FILE:-}"

if [[ -z "${CERT_FILE}" ]]; then
  CERT_FILE="$(_find_single_cert_file "*-cert.pem" "certificado TLS")"
fi

if [[ -z "${KEY_FILE}" ]]; then
  KEY_FILE="$(_find_single_cert_file "*-key.pem" "clave TLS")"
fi

[[ -f "${CERT_FILE}" ]] || _die "Certificado TLS no encontrado: ${CERT_FILE}"
[[ -f "${KEY_FILE}" ]]  || _die "Clave TLS no encontrada: ${KEY_FILE}"

# -----------------------------------------------------------------------------
# 3) Arranque Uvicorn (HTTPS only)
# -----------------------------------------------------------------------------
echo "Starting Uvicorn (HTTPS only)"
echo "  APP_MODULE=${APP_MODULE}"
echo "  HOST=${HOST}"
echo "  PORT=${PORT}"
echo "  CERT_FILE=${CERT_FILE}"
echo "  KEY_FILE=${KEY_FILE}"
echo "  RELOAD=${RELOAD}"
echo "  EXTRA_ARGS=${EXTRA_ARGS}"

if [[ "${RELOAD}" == "1" ]]; then
  exec uvicorn "${APP_MODULE}" \
    --host "${HOST}" \
    --port "${PORT}" \
    --reload \
    --ssl-certfile "${CERT_FILE}" \
    --ssl-keyfile "${KEY_FILE}" \
    ${EXTRA_ARGS}
else
  exec uvicorn "${APP_MODULE}" \
    --host "${HOST}" \
    --port "${PORT}" \
    --ssl-certfile "${CERT_FILE}" \
    --ssl-keyfile "${KEY_FILE}" \
    ${EXTRA_ARGS}
fi
