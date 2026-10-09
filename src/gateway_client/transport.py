"""Portable HTTP transport; depends only on httpx and the Python standard library."""
from uuid import uuid4
from typing import Any
import httpx


def normalize_base_url(value: str) -> str:
    """Accept service roots and reverse-proxy API prefixes ending in /v1."""
    value = value.strip().rstrip("/")
    return value if not value or value.endswith("/v1") else value + "/v1"


class GatewayTransport:
    """Apply Bearer auth, bounded timeouts and a trace to every Gateway request.

    Transport errors propagate to capability adapters for operation-specific
    error mapping and retry policy. No payload or credentials are logged.
    """
    def __init__(self, base_url: str, api_key: str, *, timeout: float = 120,
                 transport: httpx.AsyncBaseTransport | None = None) -> None:
        self.base_url = normalize_base_url(base_url)
        self.api_key = api_key
        self.timeout = timeout
        self.transport = transport

    async def request(self, method: str, path: str, *, json: Any = None,
                      params: dict[str, Any] | None = None,
                      trace_id: str | None = None) -> httpx.Response:
        async with httpx.AsyncClient(
            timeout=httpx.Timeout(self.timeout, connect=min(5, self.timeout)),
            trust_env=False, transport=self.transport,
        ) as client:
            return await client.request(
                method, self.base_url + path, json=json, params=params,
                headers={"Authorization": f"Bearer {self.api_key}",
                         "Content-Type": "application/json", "Accept": "application/json",
                         "X-Trace-ID": trace_id or f"gateway_{uuid4().hex}"},
            )
