"""Redis-backed broadcast bus for multi-instance terminal feeds."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .terminal_feed import TerminalFeed

logger = logging.getLogger(__name__)


class RedisTerminalFeedBus:
    """Bridge local terminal events across gateway replicas via Redis pub/sub."""

    def __init__(
        self,
        *,
        feed: TerminalFeed,
        redis_url: str,
        channel: str,
        instance_id: str,
        connect_timeout_seconds: float = 5.0,
    ):
        self._feed = feed
        self._redis_url = redis_url
        self._channel = channel
        self._instance_id = instance_id
        self._connect_timeout_seconds = connect_timeout_seconds
        self._client: Any = None
        self._pubsub: Any = None
        self._task: asyncio.Task | None = None
        self._running = False

    async def start(self) -> None:
        if self._task is not None:
            return
        self._running = True
        self._task = asyncio.create_task(self._run_loop())

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        await self._close_resources()

    async def publish_event(self, event: dict) -> None:
        if not self._client:
            return
        payload = json.dumps(event, separators=(",", ":"))
        await self._client.publish(self._channel, payload)

    async def _run_loop(self) -> None:
        backoff = 1.0
        while self._running:
            try:
                await self._connect()
                logger.info("Redis terminal bus connected on channel=%s", self._channel)
                backoff = 1.0
                while self._running:
                    message = await self._pubsub.get_message(timeout=1.0)
                    if not message:
                        await asyncio.sleep(0.05)
                        continue
                    if message.get("type") != "message":
                        continue
                    data = message.get("data")
                    if not isinstance(data, str):
                        continue
                    try:
                        event = json.loads(data)
                    except json.JSONDecodeError:
                        continue
                    if not isinstance(event, dict):
                        continue
                    if str(event.get("instance", "")) == self._instance_id:
                        continue
                    self._feed.ingest_external_event(event)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning("Redis terminal bus unavailable: %s", e)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 15.0)
            finally:
                await self._close_resources()

    async def _connect(self) -> None:
        try:
            import redis.asyncio as redis_async
        except Exception as e:
            raise RuntimeError("redis-py is required for SYNAPSE_TERMINAL_FEED_BUS_MODE=redis") from e

        self._client = redis_async.from_url(
            self._redis_url,
            decode_responses=True,
            socket_connect_timeout=self._connect_timeout_seconds,
            socket_timeout=self._connect_timeout_seconds,
        )
        await self._client.ping()
        self._pubsub = self._client.pubsub(ignore_subscribe_messages=True)
        await self._pubsub.subscribe(self._channel)

    async def _close_resources(self) -> None:
        if self._pubsub is not None:
            try:
                await self._pubsub.close()
            except Exception:
                pass
            self._pubsub = None
        if self._client is not None:
            try:
                await self._client.aclose()
            except Exception:
                pass
            self._client = None
