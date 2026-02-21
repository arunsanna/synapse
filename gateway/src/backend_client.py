import asyncio
import logging
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass, field

import httpx

logger = logging.getLogger(__name__)

# Timeout presets per backend type (seconds)
TIMEOUTS = {
    "llm": 300.0,
    "embeddings": 60.0,
    "tts": 120.0,
    "stt": 600.0,
    "speaker": 600.0,
    "audio": 600.0,
    "default": 60.0,
}


@dataclass
class CircuitBreaker:
    """Simple circuit breaker: open after N failures, half-open after cooldown."""

    threshold: int = 5
    cooldown: float = 30.0
    failure_count: int = field(default=0, init=False)
    last_failure: float = field(default=0.0, init=False)
    state: str = field(default="closed", init=False)  # closed | open | half-open

    def record_failure(self) -> None:
        self.failure_count += 1
        self.last_failure = time.monotonic()
        if self.failure_count >= self.threshold:
            self.state = "open"
            logger.warning("Circuit breaker OPEN after %d failures", self.failure_count)

    def record_success(self) -> None:
        self.failure_count = 0
        self.state = "closed"

    def allow_request(self) -> bool:
        if self.state == "closed":
            return True
        if self.state == "open":
            if time.monotonic() - self.last_failure > self.cooldown:
                self.state = "half-open"
                return True
            return False
        # half-open: allow one probe
        return True


class BackendClient:
    """Async HTTP client with retry and circuit breaker per backend."""

    def __init__(self) -> None:
        self._client: httpx.AsyncClient | None = None
        self._breakers: dict[str, CircuitBreaker] = {}

    async def start(self) -> None:
        self._client = httpx.AsyncClient(
            follow_redirects=True,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
        )

    async def stop(self) -> None:
        if self._client:
            await self._client.aclose()
            self._client = None

    def _breaker(self, backend: str) -> CircuitBreaker:
        if backend not in self._breakers:
            self._breakers[backend] = CircuitBreaker()
        return self._breakers[backend]

    def _require_client(self) -> httpx.AsyncClient:
        """Return initialized client or raise a clear runtime error."""
        if self._client is None:
            raise RuntimeError("Backend client is not started")
        return self._client

    async def request(
        self,
        backend_name: str,
        method: str,
        url: str,
        *,
        timeout_type: str = "default",
        max_retries: int = 3,
        **kwargs,
    ) -> httpx.Response:
        """Send request with retry + circuit breaker."""
        breaker = self._breaker(backend_name)
        if not breaker.allow_request():
            raise httpx.ConnectError(
                f"Circuit breaker open for {backend_name}"
            )

        timeout = TIMEOUTS.get(timeout_type, TIMEOUTS["default"])
        delays = [0.5, 1.0, 2.0]

        last_exc: Exception | None = None
        for attempt in range(max_retries):
            try:
                resp = await self._require_client().request(
                    method, url, timeout=timeout, **kwargs
                )
                breaker.record_success()
                return resp
            except (httpx.ConnectError, httpx.ConnectTimeout) as e:
                last_exc = e
                breaker.record_failure()
                if attempt < max_retries - 1:
                    delay = delays[min(attempt, len(delays) - 1)]
                    logger.warning(
                        "%s attempt %d failed: %s (retry in %.1fs)",
                        backend_name, attempt + 1, e, delay,
                    )
                    await asyncio.sleep(delay)

        raise last_exc

    async def stream_bytes(
        self,
        backend_name: str,
        method: str,
        url: str,
        *,
        timeout_type: str = "default",
        **kwargs,
    ) -> AsyncIterator[bytes]:
        """Open a streaming request and yield bytes.

        httpx.AsyncClient.stream() returns an async context manager, not a
        response. This method manages the lifecycle: the stream stays open
        while the caller iterates, then closes automatically.
        No retry — streams are not idempotent.
        """
        breaker = self._breaker(backend_name)
        if not breaker.allow_request():
            raise httpx.ConnectError(
                f"Circuit breaker open for {backend_name}"
            )

        timeout = TIMEOUTS.get(timeout_type, TIMEOUTS["default"])
        try:
            async with self._require_client().stream(
                method, url, timeout=timeout, **kwargs
            ) as resp:
                breaker.record_success()
                async for chunk in resp.aiter_bytes():
                    yield chunk
        except (httpx.ConnectError, httpx.ConnectTimeout) as e:
            breaker.record_failure()
            raise

    async def health_check(self, backend_name: str, url: str) -> dict:
        """Check a backend's health endpoint. Returns status dict."""
        try:
            resp = await self._require_client().get(url, timeout=5.0)
            return {
                "status": "healthy" if resp.status_code == 200 else "unhealthy",
                "code": resp.status_code,
            }
        except Exception as e:
            return {"status": "unreachable", "error": str(e)}


# Singleton
client = BackendClient()
