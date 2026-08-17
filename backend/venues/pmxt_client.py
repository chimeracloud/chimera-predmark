"""HTTP client for the self-hosted pmxt sidecar.

The sidecar is pmxt-core running in this container, started by the
entrypoint. Its contract, verified against pmxt-core 2.54.0:

    POST /api/{exchange}/{method}
    headers: x-pmxt-access-token: <shared token>
    body:    {"args": [...], "credentials": {...}}
    ->       {"success": true, "data": ...}
             {"success": false, "error": {"message", "code", "retryable"}}

Credentials travel in the request body to localhost only. They are never
logged, never cached to disk, and are held in memory only for the duration of
a call.

The `router` pseudo-exchange is refused: it is the one part of pmxt-core that
calls api.pmxt.dev, and this build is self-hosted by requirement.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Optional

import httpx

import config
from logging_setup import log

logger = logging.getLogger(__name__)

FORBIDDEN_EXCHANGES = {"router", "mock"}


class PmxtError(RuntimeError):
    """A call to the sidecar failed."""

    def __init__(
        self,
        message: str,
        *,
        code: str = "UNKNOWN",
        retryable: bool = False,
        status: int = 0,
        exchange: str = "",
        method: str = "",
    ) -> None:
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.status = status
        self.exchange = exchange
        self.method = method

    def to_dict(self) -> dict[str, Any]:
        return {
            "message": str(self),
            "code": self.code,
            "retryable": self.retryable,
            "status": self.status,
            "exchange": self.exchange,
            "method": self.method,
        }


class PmxtUnavailable(PmxtError):
    """The sidecar itself is not answering — distinct from a venue error."""


class PmxtClient:
    """Async client against the local sidecar.

    One instance per process; it owns a connection pool and a concurrency
    semaphore so a wide scan cannot open hundreds of sockets at once.
    """

    def __init__(
        self,
        base_url: str | None = None,
        access_token: str | None = None,
        timeout: float | None = None,
        concurrency: int | None = None,
    ) -> None:
        self.base_url = (base_url or config.PMXT_URL).rstrip("/")
        self.access_token = (
            access_token if access_token is not None else config.PMXT_ACCESS_TOKEN
        )
        self.timeout = timeout or config.PMXT_TIMEOUT_SECONDS
        self._client: Optional[httpx.AsyncClient] = None
        self._semaphore = asyncio.Semaphore(concurrency or config.VENUE_CONCURRENCY)

    async def __aenter__(self) -> "PmxtClient":
        await self.start()
        return self

    async def __aexit__(self, *_: Any) -> None:
        await self.close()

    async def start(self) -> None:
        if self._client is None:
            self._client = httpx.AsyncClient(
                base_url=self.base_url,
                timeout=httpx.Timeout(self.timeout, connect=10.0),
                limits=httpx.Limits(max_connections=32, max_keepalive_connections=16),
                headers={"content-type": "application/json"},
            )

    async def close(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    @property
    def _headers(self) -> dict[str, str]:
        return {"x-pmxt-access-token": self.access_token} if self.access_token else {}

    async def health(self) -> bool:
        """Is the sidecar up? Public endpoint, no token needed."""
        await self.start()
        assert self._client is not None
        try:
            response = await self._client.get("/health", timeout=5.0)
            return response.status_code == 200
        except httpx.HTTPError:
            return False

    async def wait_until_ready(self, attempts: int = 30, delay: float = 1.0) -> bool:
        """Block until the sidecar answers /health.

        Called at app startup: Node takes a second or two longer to boot than
        uvicorn, and a scan arriving in that window should wait rather than
        fail.
        """
        for _ in range(attempts):
            if await self.health():
                return True
            await asyncio.sleep(delay)
        return False

    async def call(
        self,
        exchange: str,
        method: str,
        args: Optional[list[Any]] = None,
        credentials: Optional[dict[str, Any]] = None,
        timeout: Optional[float] = None,
        retries: int = 2,
    ) -> Any:
        """Invoke `exchange[method](*args)` on the sidecar.

        Retries only on errors the sidecar marks retryable, or on transport
        failures. Order placement passes retries=0 — a timed-out order may
        well have reached the venue, and blindly resending it is how you end
        up with two positions where you wanted one.
        """
        if exchange in FORBIDDEN_EXCHANGES:
            raise PmxtError(
                f"exchange '{exchange}' is not permitted: this deployment is "
                "self-hosted and must not route through pmxt.dev",
                code="FORBIDDEN_EXCHANGE",
                exchange=exchange,
                method=method,
            )

        await self.start()
        assert self._client is not None

        body: dict[str, Any] = {"args": args or []}
        if credentials:
            body["credentials"] = credentials

        last_error: Optional[PmxtError] = None
        for attempt in range(retries + 1):
            try:
                async with self._semaphore:
                    response = await self._client.post(
                        f"/api/{exchange}/{method}",
                        json=body,
                        headers=self._headers,
                        timeout=timeout or self.timeout,
                    )
            except httpx.HTTPError as exc:
                last_error = PmxtUnavailable(
                    f"sidecar transport error: {type(exc).__name__}",
                    code="SIDECAR_UNAVAILABLE",
                    retryable=True,
                    exchange=exchange,
                    method=method,
                )
            else:
                try:
                    payload = response.json()
                except ValueError:
                    payload = {
                        "success": False,
                        "error": {"message": response.text[:400]},
                    }

                if response.status_code == 200 and payload.get("success"):
                    return payload.get("data")

                error = payload.get("error")
                if isinstance(error, str):
                    error = {"message": error}
                error = error or {}
                last_error = PmxtError(
                    str(error.get("message", "unknown sidecar error")),
                    code=str(error.get("code", f"HTTP_{response.status_code}")),
                    retryable=bool(error.get("retryable", False)),
                    status=response.status_code,
                    exchange=exchange,
                    method=method,
                )
                # 4xx other than rate limiting will not improve on retry.
                if response.status_code < 500 and response.status_code != 429:
                    if not last_error.retryable:
                        raise last_error

            if attempt < retries and last_error and last_error.retryable:
                await asyncio.sleep(min(2**attempt, 8) * 0.5)
                continue
            break

        assert last_error is not None
        log(
            logger,
            logging.WARNING,
            "pmxt call failed",
            exchange=exchange,
            method=method,
            code=last_error.code,
            status=last_error.status,
        )
        raise last_error
