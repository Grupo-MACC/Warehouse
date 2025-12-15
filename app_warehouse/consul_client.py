# -*- coding: utf-8 -*-
"""Consul client for service discovery and registration."""
import os
import logging
import asyncio
import httpx

logger = logging.getLogger(__name__)


class ConsulClient:
    """Client for interacting with Consul service discovery."""

    def __init__(self, host: str = "localhost", port: int = 8500):
        self.host = host
        self.port = port
        self.base_url = f"http://{host}:{port}/v1"

    async def register_service(
        self,
        service_name: str,
        service_id: str,
        service_port: int,
        service_address: str,
        tags: list = None,
        meta: dict = None,
        health_check_url: str = None,
    ) -> bool:
        """Register service with Consul using HTTP health check."""
        payload = {
            "ID": service_id,
            "Name": service_name,
            "Address": service_address,
            "Port": service_port,
            "Tags": tags or [],
            "Meta": meta or {},
        }

        # HTTP health check (ahora funciona porque no hay TLS interno)
        payload["Check"] = {
            "HTTP": health_check_url or f"http://{service_address}:{service_port}/docs",
            "Interval": "10s",
            "Timeout": "5s",
            "DeregisterCriticalServiceAfter": "30s",
        }

        try:
            async with httpx.AsyncClient() as client:
                response = await client.put(
                    f"{self.base_url}/agent/service/register",
                    json=payload,
                    timeout=10.0,
                )
                if response.status_code == 200:
                    logger.info(f"✅ Service {service_name} registered successfully")
                    return True
                else:
                    logger.error(
                        f"❌ Failed to register service: {response.status_code} - {response.text}"
                    )
                    return False
        except Exception as e:
            logger.error(f"❌ Error registering service: {e}")
            return False

    async def deregister_service(self, service_id: str) -> bool:
        """Deregister service from Consul."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.put(
                    f"{self.base_url}/agent/service/deregister/{service_id}",
                    timeout=10.0,
                )
                if response.status_code == 200:
                    logger.info(f"✅ Service {service_id} deregistered successfully")
                    return True
                else:
                    logger.error(
                        f"❌ Failed to deregister service: {response.status_code}"
                    )
                    return False
        except Exception as e:
            logger.error(f"❌ Error deregistering service: {e}")
            return False

    async def discover_service(self, service_name: str) -> dict:
        """Discover service location from Consul."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/catalog/service/{service_name}",
                    timeout=10.0,
                )
                if response.status_code == 200:
                    services = response.json()
                    if services:
                        service = services[0]
                        return {
                            "address": service.get("ServiceAddress") or service.get("Address"),
                            "port": service.get("ServicePort"),
                        }
        except Exception as e:
            logger.error(f"❌ Error discovering service {service_name}: {e}")
        return None

    async def get_healthy_services(self, service_name: str) -> list:
        """Get list of healthy service instances."""
        try:
            async with httpx.AsyncClient() as client:
                response = await client.get(
                    f"{self.base_url}/health/service/{service_name}?passing=true",
                    timeout=10.0,
                )
                if response.status_code == 200:
                    return response.json()
        except Exception as e:
            logger.error(f"❌ Error getting healthy services: {e}")
        return []


# Global consul client instance
_consul_client = None


def create_consul_client() -> ConsulClient:
    """Create and return a Consul client instance."""
    global _consul_client
    if _consul_client is None:
        host = os.getenv("CONSUL_HOST", "localhost")
        port = int(os.getenv("CONSUL_PORT", 8500))
        _consul_client = ConsulClient(host, port)
    return _consul_client


async def get_service_url(service_name: str, default_url: str = None) -> str:
    """Get service URL from Consul with retry and fallback."""
    consul_client = create_consul_client()
    max_retries = 5
    retry_delay = 1

    for attempt in range(max_retries):
        try:
            service_info = await consul_client.discover_service(service_name)
            if service_info:
                # HTTP interno (sin TLS)
                url = f"http://{service_info['address']}:{service_info['port']}"
                logger.info(f"✅ Service {service_name} discovered at {url}")
                return url
        except Exception as e:
            logger.warning(
                f"Attempt {attempt + 1}/{max_retries}: Could not find {service_name}: {e}"
            )
        if attempt < max_retries - 1:
            await asyncio.sleep(retry_delay)

    # Fallback to default
    if default_url:
        logger.warning(f"⚠️ Using fallback URL for {service_name}: {default_url}")
        return default_url

    raise Exception(f"Could not discover service: {service_name}")
