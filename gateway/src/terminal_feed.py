"""Live terminal feed service with redaction and bounded fanout queues."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import threading
from collections import deque
from datetime import datetime, timezone
from typing import Awaitable, Callable


_LEVEL_RANK = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


class LogRedactor:
    """Redact common credential patterns before events reach the browser."""

    def __init__(self, extra_patterns: str = ""):
        self._regex_replacements: list[tuple[re.Pattern[str], str]] = [
            (
                re.compile(r"(?i)\b(authorization)\s*:\s*bearer\s+[a-z0-9._\-+/=]+"),
                r"\1: Bearer [REDACTED]",
            ),
            (
                re.compile(r"(?i)\bbearer\s+[a-z0-9._\-+/=]+"),
                "Bearer [REDACTED]",
            ),
            (
                re.compile(r'(?i)("?(?:api[-_]?key|token|secret|password|passwd|cookie)"?\s*[:=]\s*)(".*?"|[^,\s;]+)'),
                r"\1[REDACTED]",
            ),
        ]
        self._extra_regex: list[re.Pattern[str]] = []
        for raw in (extra_patterns or "").split("||"):
            pattern = raw.strip()
            if not pattern:
                continue
            try:
                self._extra_regex.append(re.compile(pattern))
            except re.error:
                # Invalid custom regex should not break log streaming.
                continue

    def redact(self, text: str) -> str:
        out = text
        for regex, repl in self._regex_replacements:
            out = regex.sub(repl, out)
        for regex in self._extra_regex:
            out = regex.sub("[REDACTED]", out)
        return out


class TerminalFeed:
    """In-process terminal feed with bounded history and non-blocking subscribers."""

    def __init__(
        self,
        *,
        buffer_size: int,
        subscriber_queue_size: int,
        max_line_chars: int,
        instance_id: str,
        redactor: LogRedactor,
    ):
        self._buffer: deque[dict] = deque(maxlen=max(10, buffer_size))
        self._subscribers: set[asyncio.Queue[dict]] = set()
        self._subscriber_queue_size = max(10, subscriber_queue_size)
        self._max_line_chars = max(256, max_line_chars)
        self._instance_id = instance_id
        self._redactor = redactor
        self._dropped_events = 0
        self._loop: asyncio.AbstractEventLoop | None = None
        self._loop_thread_ident: int | None = None
        self._handler: logging.Handler | None = None
        self._distributor: Callable[[dict], Awaitable[None]] | None = None
        self._distributed_publish_failures = 0

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._loop_thread_ident = threading.get_ident()

    def stop(self) -> None:
        self._subscribers.clear()
        self._loop = None
        self._loop_thread_ident = None
        self._distributor = None

    def attach_handler(self, logger: logging.Logger) -> None:
        if self._handler is not None:
            return
        handler = _TerminalFeedHandler(self)
        logger.addHandler(handler)
        self._handler = handler

    def detach_handler(self, logger: logging.Logger) -> None:
        if self._handler is None:
            return
        logger.removeHandler(self._handler)
        self._handler = None

    def publish_record(self, record: logging.LogRecord) -> None:
        source = record.name or "gateway"
        level = record.levelname if record.levelname in _LEVEL_RANK else "INFO"
        message = record.getMessage()
        self.publish_message(source=source, level=level, message=message)

    def publish_message(self, *, source: str, level: str, message: str) -> None:
        event = self._build_event(source=source, level=level, message=message)
        loop = self._loop
        if loop is None or not loop.is_running():
            self._buffer.append(event)
            return
        if threading.get_ident() == self._loop_thread_ident:
            self._publish_now(event, distribute=True)
            return
        loop.call_soon_threadsafe(self._publish_now, event, True)

    def ingest_external_event(self, raw_event: dict) -> None:
        event = self._normalize_external_event(raw_event)
        loop = self._loop
        if loop is None or not loop.is_running():
            self._buffer.append(event)
            return
        if threading.get_ident() == self._loop_thread_ident:
            self._publish_now(event, distribute=False)
            return
        loop.call_soon_threadsafe(self._publish_now, event, False)

    def set_distributor(self, distributor: Callable[[dict], Awaitable[None]] | None) -> None:
        self._distributor = distributor

    def subscribe(self) -> asyncio.Queue[dict]:
        queue: asyncio.Queue[dict] = asyncio.Queue(maxsize=self._subscriber_queue_size)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict]) -> None:
        self._subscribers.discard(queue)

    def backlog(self, *, limit: int, min_level: str, allowed_sources: set[str] | None) -> list[dict]:
        bounded_limit = max(1, min(limit, len(self._buffer)))
        min_rank = _LEVEL_RANK.get(min_level.upper(), _LEVEL_RANK["INFO"])
        items = []
        for event in reversed(self._buffer):
            if _LEVEL_RANK.get(event["level"], 20) < min_rank:
                continue
            if allowed_sources and event["source"] not in allowed_sources:
                continue
            items.append(event)
            if len(items) >= bounded_limit:
                break
        items.reverse()
        return items

    def stats(self) -> dict:
        return {
            "buffer_size": len(self._buffer),
            "subscriber_count": len(self._subscribers),
            "dropped_events": self._dropped_events,
            "distributed_publish_failures": self._distributed_publish_failures,
        }

    def _build_event(self, *, source: str, level: str, message: str) -> dict:
        clean_source = (source or "gateway").strip()[:120]
        text = self._sanitize_message(message)
        now = datetime.now(timezone.utc)
        return {
            "ts": now.isoformat(),
            "source": clean_source,
            "level": level.upper(),
            "message": text,
            "instance": self._instance_id,
        }

    def _normalize_external_event(self, raw_event: dict) -> dict:
        source = str(raw_event.get("source", "external")).strip()[:120]
        level = str(raw_event.get("level", "INFO")).upper()
        if level not in _LEVEL_RANK:
            level = "INFO"
        instance = str(raw_event.get("instance", "external")).strip()[:120]
        ts = str(raw_event.get("ts", "")).strip()[:64]
        if not ts:
            ts = datetime.now(timezone.utc).isoformat()
        return {
            "ts": ts,
            "source": source,
            "level": level,
            "message": self._sanitize_message(raw_event.get("message", "")),
            "instance": instance,
        }

    def _sanitize_message(self, message: object) -> str:
        text = self._redactor.redact(str(message).replace("\r", " ").replace("\n", " \\n "))
        if len(text) > self._max_line_chars:
            return f"{text[: self._max_line_chars - 12]}...[truncated]"
        return text

    async def _run_distributor(self, event: dict) -> None:
        if self._distributor is None:
            return
        try:
            await self._distributor(event)
        except Exception:
            self._distributed_publish_failures += 1

    def _publish_now(self, event: dict, distribute: bool = True) -> None:
        self._buffer.append(event)
        stale_subscribers: list[asyncio.Queue[dict]] = []
        for queue in self._subscribers:
            if queue.full():
                try:
                    queue.get_nowait()
                    self._dropped_events += 1
                except asyncio.QueueEmpty:
                    pass
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._dropped_events += 1
                stale_subscribers.append(queue)
        for queue in stale_subscribers:
            self._subscribers.discard(queue)
        if distribute and self._distributor is not None:
            asyncio.create_task(self._run_distributor(event))


class _TerminalFeedHandler(logging.Handler):
    """Logging handler that forwards records into the terminal feed."""

    def __init__(self, feed: TerminalFeed):
        super().__init__(level=logging.INFO)
        self._feed = feed

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._feed.publish_record(record)
        except Exception:
            # Never raise from logging handlers.
            return


def parse_source_filter(raw_sources: str | None) -> set[str] | None:
    if not raw_sources:
        return None
    values = {item.strip() for item in raw_sources.split(",") if item.strip()}
    return values or None


def validate_level(raw_level: str | None, default: str = "INFO") -> str:
    candidate = (raw_level or default or "INFO").upper()
    return candidate if candidate in _LEVEL_RANK else "INFO"


def as_sse(event_name: str, payload: dict) -> str:
    return f"event: {event_name}\ndata: {json.dumps(payload, separators=(',', ':'))}\n\n"
