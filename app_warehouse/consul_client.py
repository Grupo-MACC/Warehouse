from __future__ import annotations

import os
import json
import time
import random
import logging
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)


# -----------------------------------------------------------------------------
# Helpers de parsing de entorno
# -----------------------------------------------------------------------------
def _env(name: str, default: Optional[str] = None) -> Optional[str]:
    """Lee una variable de entorno y normaliza strings vacíos a None."""
    value = os.getenv(name, default)
    if value is None:
        return None
    value = value.strip()
    return value if value else None


def _env_bool(name: str, default: bool) -> bool:
    """
    Parsea booleanos típicos desde entorno.

    Acepta: 1/0, true/false, yes/no, on/off (case-insensitive).
    """
    raw = _env(name)
    if raw is None:
        return default
    return raw.lower() in {"1", "true", "yes", "y", "on"}


def _env_int(name: str, default: int) -> int:
    """Parsea entero desde entorno con default seguro."""
    raw = _env(name)
    if raw is None:
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    """Parsea float desde entorno con default seguro."""
    raw = _env(name)
    if raw is None:
        return default
    return float(raw)


def _parse_csv(value: Optional[str]) -> List[str]:
    """Convierte 'a,b,c' -> ['a','b','c'] ignorando vacíos."""
    if not value:
        return []
    items = []
    for part in value.split(","):
        part = part.strip()
        if part:
            items.append(part)
    return items


def _parse_meta(value: Optional[str]) -> Dict[str, str]:
    """
    Parsea meta desde:
    - JSON: '{"version":"1.0.0","team":"grupo2"}'
    - o estilo 'k=v,k2=v2'
    """
    if not value:
        return {}

    value = value.strip()
    if value.startswith("{"):
        obj = json.loads(value)
        return {str(k): str(v) for k, v in obj.items()}

    meta: Dict[str, str] = {}
    for pair in _parse_csv(value):
        if "=" not in pair:
            continue
        k, v = pair.split("=", 1)
        k, v = k.strip(), v.strip()
        if k:
            meta[k] = v
    return meta


# -----------------------------------------------------------------------------
# Settings
# -----------------------------------------------------------------------------
@dataclass(frozen=True)
class ConsulSettings:
    """
    Configuración de conexión a Consul (cliente).

    Estricto:
    - scheme debe ser 'https'
    - ca_file es obligatorio
    """
    host: str
    port: int
    scheme: str
    ca_file: str
    timeout: float
    token: Optional[str]
    require_https: bool

    @classmethod
    def from_env(cls) -> "ConsulSettings":
        """
        Crea settings desde variables de entorno.

        Compatibilidad:
        - Si existe CONSUL_HTTP_ADDR, se puede usar (formato host:port o https://host:port).
        - CONSUL_CACERT como alternativa a CONSUL_CA_FILE.
        - CONSUL_HTTP_TOKEN como alternativa a CONSUL_TOKEN.
        """
        # Defaults pensados para tu caso (Consul TLS en 8501).
        host = _env("CONSUL_HOST", "consul") or "consul"
        port = _env_int("CONSUL_PORT", 8501)
        scheme = (_env("CONSUL_SCHEME", "https") or "https").lower()

        # Compat: CONSUL_HTTP_ADDR
        http_addr = _env("CONSUL_HTTP_ADDR")
        if http_addr:
            # Permite "https://consul:8501" o "consul:8501"
            addr = http_addr.replace("https://", "").replace("http://", "")
            if ":" in addr:
                host, port_s = addr.split(":", 1)
                host = host.strip() or host
                port = int(port_s.strip())

        ca_file = _env("CONSUL_CA_FILE") or _env("CONSUL_CACERT")
        token = _env("CONSUL_TOKEN") or _env("CONSUL_HTTP_TOKEN")
        timeout = _env_float("CONSUL_TIMEOUT", 10.0)
        require_https = _env_bool("CONSUL_REQUIRE_HTTPS", True)

        # En tu objetivo: SOLO HTTPS.
        if require_https and scheme != "https":
            raise ValueError(
                f"CONSUL_SCHEME='{scheme}' no permitido. Este proyecto requiere HTTPS en Consul."
            )

        if scheme == "https" and not ca_file:
            raise ValueError(
                "Falta CONSUL_CA_FILE/CONSUL_CACERT. "
                "Para HTTPS debes proporcionar la CA que firmó el certificado de Consul."
            )

        return cls(
            host=host,
            port=port,
            scheme=scheme,
            ca_file=ca_file or "",
            timeout=timeout,
            token=token,
            require_https=require_https,
        )


@dataclass(frozen=True)
class ServiceRegistration:
    """
    Configuración del servicio a registrar, derivada del entorno.

    Se fuerza SERVICE_SCHEME=https si SERVICE_REQUIRE_HTTPS=1.
    """
    name: str
    service_id: str
    address: str
    port: int
    scheme: str
    tags: List[str]
    meta: Dict[str, str]
    health_path: str
    check_interval: str
    check_timeout: str
    deregister_after: str
    require_https: bool

    @classmethod
    def from_env(cls) -> "ServiceRegistration":
        """Crea la configuración de registro desde variables de entorno del microservicio."""
        name = _env("SERVICE_NAME")
        if not name:
            raise ValueError("Falta SERVICE_NAME")

        port_raw = _env("SERVICE_PORT")
        if not port_raw:
            raise ValueError("Falta SERVICE_PORT")
        port = int(port_raw)

        hostname = _env("HOSTNAME", "unknown") or "unknown"
        service_id = _env("SERVICE_ID", f"{name}-{hostname}") or f"{name}-{hostname}"
        address = _env("SERVICE_ADDRESS", name) or name

        scheme = (_env("SERVICE_SCHEME", "https") or "https").lower()
        require_https = _env_bool("SERVICE_REQUIRE_HTTPS", True)
        if require_https and scheme != "https":
            raise ValueError(
                f"SERVICE_SCHEME='{scheme}' no permitido. Este proyecto requiere HTTPS entre servicios."
            )

        # Tags/meta (meta admite JSON o k=v,k2=v2)
        tags = _parse_csv(_env("SERVICE_TAGS")) or ["fastapi", "https", name]
        meta = _parse_meta(_env("SERVICE_META"))
        # Guardamos el esquema en meta para que discovery lo pueda inferir.
        meta.setdefault("scheme", scheme)

        health_path = _env("SERVICE_HEALTH_PATH", "/health") or "/health"
        check_interval = _env("SERVICE_CHECK_INTERVAL", "10s") or "10s"
        check_timeout = _env("SERVICE_CHECK_TIMEOUT", "5s") or "5s"
        deregister_after = _env("SERVICE_DEREGISTER_AFTER", "30s") or "30s"

        return cls(
            name=name,
            service_id=service_id,
            address=address,
            port=port,
            scheme=scheme,
            tags=tags,
            meta=meta,
            health_path=health_path,
            check_interval=check_interval,
            check_timeout=check_timeout,
            deregister_after=deregister_after,
            require_https=require_https,
        )


# -----------------------------------------------------------------------------
# Cliente principal
# -----------------------------------------------------------------------------
class ConsulClient:
    """
    Cliente asíncrono para Consul HTTP API.

    Características:
    - Usa un httpx.AsyncClient persistente (mejor rendimiento que crear uno por request).
    - HTTPS estricto hacia Consul con verificación por CA.
    - Registro/deregistro y descubrimiento saludable.

    Nota importante:
    - Este cliente protege la comunicación *microservicio -> Consul*.
    - No cifra por sí solo el tráfico *microservicio -> microservicio*; eso depende de
      cómo levantes cada servicio (Uvicorn SSL) y/o el gateway.
    """

    def __init__(self, settings: ConsulSettings) -> None:
        """Inicializa el cliente con settings validados."""
        self._settings = settings

        base_url = f"{settings.scheme}://{settings.host}:{settings.port}/v1"

        headers: Dict[str, str] = {}
        if settings.token:
            # ACL token (opcional). Esto NO es mTLS.
            headers["X-Consul-Token"] = settings.token

        self._http = httpx.AsyncClient(
            base_url=base_url,
            verify=settings.ca_file if settings.scheme == "https" else False,
            timeout=settings.timeout,
            headers=headers,
        )

    async def aclose(self) -> None:
        """Cierra el cliente HTTP (recomendado en shutdown)."""
        await self._http.aclose()

    # -------------------------
    # Registro / Deregistro
    # -------------------------
    async def register_self(self) -> bool:
        """
        Registra el servicio actual en Consul leyendo env vars.

        Ventaja:
        - El main de cada microservicio se reduce a una llamada.
        """
        reg = ServiceRegistration.from_env()
        return await self.register_service(reg)

    async def register_service(self, reg: ServiceRegistration) -> bool:
        """
        Registra un servicio en Consul (agent/service/register).

        Check:
        - Se construye un HTTP check hacia el propio servicio usando reg.scheme + reg.health_path.
        - Si reg.scheme es https, el check se registra como HTTPS.

        Importante:
        - Para que Consul valide el certificado del microservicio en el check HTTPS,
          el contenedor de Consul debe confiar en la CA que firmó ese certificado.
        """
        check_url = f"{reg.scheme}://{reg.address}:{reg.port}{reg.health_path}"

        payload: Dict[str, Any] = {
            "ID": reg.service_id,
            "Name": reg.name,
            "Address": reg.address,
            "Port": reg.port,
            "Tags": reg.tags,
            "Meta": reg.meta,
            "Check": {
                "HTTP": check_url,
                "Interval": reg.check_interval,
                "Timeout": reg.check_timeout,
                "DeregisterCriticalServiceAfter": reg.deregister_after,
            },
        }

        try:
            resp = await self._http.put("/agent/service/register", json=payload)
            if resp.status_code == 200:
                logger.info("✅ Registrado en Consul: %s (%s)", reg.name, reg.service_id)
                return True

            logger.error("❌ Registro Consul fallido (%s): %s", resp.status_code, resp.text)
            return False

        except Exception:
            logger.exception("❌ Excepción registrando servicio en Consul")
            return False

    async def deregister_self(self) -> bool:
        """Desregistra el servicio actual desde env vars."""
        reg = ServiceRegistration.from_env()
        return await self.deregister_service(reg.service_id)

    async def deregister_service(self, service_id: str) -> bool:
        """Elimina un servicio de Consul (agent/service/deregister)."""
        try:
            resp = await self._http.put(f"/agent/service/deregister/{service_id}")
            if resp.status_code == 200:
                logger.info("✅ Desregistrado de Consul: %s", service_id)
                return True

            logger.error("❌ Deregistro Consul fallido (%s): %s", resp.status_code, resp.text)
            return False

        except Exception:
            logger.exception("❌ Excepción desregistrando servicio en Consul")
            return False

    # -------------------------
    # Descubrimiento
    # -------------------------
    async def resolve_service(
        self,
        service_name: str,
        passing_only: bool = True,
    ) -> Optional[Dict[str, Any]]:
        """
        Descubre una instancia del servicio.

        Usa health API (recomendado):
        - /v1/health/service/<name>?passing=true
        Devuelve la estructura del servicio y permite decidir esquema.

        Selección:
        - Elige aleatoriamente entre instancias disponibles (simple, útil con réplicas).
        """
        params = {"passing": "true"} if passing_only else None
        try:
            resp = await self._http.get(f"/health/service/{service_name}", params=params)
            if resp.status_code != 200:
                return None

            items = resp.json()
            if not items:
                return None

            chosen = random.choice(items)
            svc = chosen.get("Service", {}) or {}
            node = chosen.get("Node", {}) or {}

            address = svc.get("Address") or node.get("Address")
            port = svc.get("Port")
            tags = svc.get("Tags") or []
            meta = svc.get("Meta") or {}

            if not address or not port:
                return None

            return {
                "address": address,
                "port": int(port),
                "tags": tags,
                "meta": meta,
            }

        except Exception:
            logger.exception("❌ Excepción resolviendo servicio '%s' en Consul", service_name)
            return None

    async def get_service_base_url(self, service_name: str) -> str:
        """
        Devuelve base_url del servicio descubierto.

        Reglas de esquema:
        - Si meta['scheme'] existe, se usa.
        - Si no, se usa DISCOVERY_DEFAULT_SCHEME (default: https).
        - Si DISCOVERY_REQUIRE_HTTPS=1 y el esquema resultante no es https -> error.
        """
        discovery_default_scheme = (_env("DISCOVERY_DEFAULT_SCHEME", "https") or "https").lower()
        discovery_require_https = _env_bool("DISCOVERY_REQUIRE_HTTPS", True)

        svc = await self.resolve_service(service_name, passing_only=True)
        if not svc:
            raise RuntimeError(f"No se encontró servicio saludable en Consul: {service_name}")

        meta = svc.get("meta") or {}
        scheme = (meta.get("scheme") or discovery_default_scheme).lower()

        if discovery_require_https and scheme != "https":
            raise RuntimeError(
                f"Servicio '{service_name}' descubierto con scheme='{scheme}'. "
                "Este proyecto requiere HTTPS entre servicios."
            )

        return f"{scheme}://{svc['address']}:{svc['port']}"


# -----------------------------------------------------------------------------
# Singleton (estilo actual, pero listo para chassis)
# -----------------------------------------------------------------------------
_consul_client: Optional[ConsulClient] = None


def get_consul_client() -> ConsulClient:
    """
    Devuelve singleton de ConsulClient.

    Uso típico:
    - En lifespan startup: await get_consul_client().register_self()
    - En shutdown: await get_consul_client().deregister_self()
    """
    global _consul_client
    if _consul_client is None:
        settings = ConsulSettings.from_env()
        _consul_client = ConsulClient(settings)
    return _consul_client