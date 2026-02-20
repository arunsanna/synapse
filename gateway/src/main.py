"""Synapse Gateway — unified AI proxy for ArunLabs Forge cluster."""

import asyncio
import html
import logging
import time as _time
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse

from .backend_client import client
from .config import load_backends_config, settings
from .terminal_feed import LogRedactor, TerminalFeed, as_sse, parse_source_filter, validate_level
from .terminal_feed_bus_redis import RedisTerminalFeedBus
from .voice_manager import VoiceManager

logger = logging.getLogger(__name__)

# Shared state populated at startup
_backends_config: dict = {}
_voice_manager: VoiceManager | None = None
_start_time: float = 0.0
_terminal_feed: TerminalFeed | None = None
_terminal_feed_bus: RedisTerminalFeedBus | None = None
_DASHBOARD_TEMPLATE: str = ""


def _load_dashboard_template() -> None:
    """Read dashboard.html template from disk once at startup."""
    global _DASHBOARD_TEMPLATE
    template_path = Path(__file__).parent / "templates" / "dashboard.html"
    _DASHBOARD_TEMPLATE = template_path.read_text(encoding="utf-8")


def get_backends_config() -> dict:
    return _backends_config


def get_voice_manager() -> VoiceManager:
    if _voice_manager is None:
        raise RuntimeError("Voice manager is not initialized")
    return _voice_manager


def get_terminal_feed() -> TerminalFeed:
    if _terminal_feed is None:
        raise RuntimeError("Terminal feed is not initialized")
    return _terminal_feed


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: load config, init httpx pool, init voice manager."""
    global _backends_config, _voice_manager, _start_time, _terminal_feed, _terminal_feed_bus

    logging.basicConfig(
        level=getattr(logging, settings.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    _backends_config = load_backends_config()
    logger.info(
        "Loaded %d backends from %s",
        len(_backends_config.get("backends", {})),
        settings.gateway_config_path,
    )

    _voice_manager = VoiceManager(library_dir=settings.voice_library_dir)
    _terminal_feed = TerminalFeed(
        buffer_size=settings.terminal_feed_buffer_size,
        subscriber_queue_size=settings.terminal_feed_subscriber_queue_size,
        max_line_chars=settings.terminal_feed_max_line_chars,
        instance_id=settings.instance_id,
        redactor=LogRedactor(settings.terminal_feed_redact_extra_patterns),
    )
    _terminal_feed.start(asyncio.get_running_loop())
    _terminal_feed.attach_handler(logging.getLogger())
    bus_mode = settings.terminal_feed_bus_mode.strip().lower()
    if settings.terminal_feed_mode.strip().lower() == "live" and bus_mode == "redis":
        redis_url = settings.terminal_feed_redis_url.strip()
        if not redis_url:
            raise RuntimeError("SYNAPSE_TERMINAL_FEED_REDIS_URL is required when SYNAPSE_TERMINAL_FEED_BUS_MODE=redis")
        _terminal_feed_bus = RedisTerminalFeedBus(
            feed=_terminal_feed,
            redis_url=redis_url,
            channel=settings.terminal_feed_redis_channel.strip() or "synapse:terminal_feed",
            instance_id=settings.instance_id,
            connect_timeout_seconds=settings.terminal_feed_redis_connect_timeout_seconds,
        )
        _terminal_feed.set_distributor(_terminal_feed_bus.publish_event)
        await _terminal_feed_bus.start()
        logger.info("Terminal feed bus enabled: redis channel=%s", settings.terminal_feed_redis_channel)
    else:
        _terminal_feed.set_distributor(None)
        if bus_mode not in {"", "local"}:
            logger.warning("Unknown terminal feed bus mode '%s'; falling back to local-only mode", bus_mode)
    await client.start()
    _start_time = _time.time()
    _load_dashboard_template()
    logger.info("Synapse Gateway started")

    yield

    if _terminal_feed_bus is not None:
        await _terminal_feed_bus.stop()
        _terminal_feed_bus = None
    if _terminal_feed is not None:
        _terminal_feed.set_distributor(None)
        _terminal_feed.detach_handler(logging.getLogger())
        _terminal_feed.stop()
        _terminal_feed = None
    await client.stop()
    logger.info("Synapse Gateway stopped")


app = FastAPI(title="Synapse Gateway", version="1.0.0", lifespan=lifespan)


# --- Error handling ---


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    import httpx as _httpx

    if isinstance(exc, _httpx.ConnectError):
        return JSONResponse(status_code=503, content={"error": "Backend unavailable", "detail": str(exc)})
    if isinstance(exc, (_httpx.ReadTimeout, _httpx.WriteTimeout)):
        return JSONResponse(status_code=504, content={"error": "Backend timeout", "detail": str(exc)})
    logger.exception("Unhandled error: %s", exc)
    return JSONResponse(status_code=500, content={"error": "Internal server error"})


# --- Health endpoint ---


@app.get("/health")
async def health():
    """Aggregated health check across all registered backends."""
    config = get_backends_config()
    backends = config.get("backends", {})
    results = {}

    for name, backend in backends.items():
        health_path = backend.get("health", "/health")
        url = f"{backend['url']}{health_path}"
        results[name] = await client.health_check(name, url)

    all_healthy = all(r["status"] == "healthy" for r in results.values())
    return {
        "status": "healthy" if all_healthy else "degraded",
        "backends": results,
    }


# --- Dashboard ---


def _format_uptime(seconds: float) -> str:
    s = int(seconds)
    days, s = divmod(s, 86400)
    hours, s = divmod(s, 3600)
    minutes, s = divmod(s, 60)
    parts = []
    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    parts.append(f"{s}s")
    return " ".join(parts)


def _build_backend_cards(backends: dict, health_results: dict) -> str:
    cards = []
    for name, backend in backends.items():
        info = health_results.get(name, {})
        status = info.get("status", "checking")
        label = status.capitalize()
        if info.get("code"):
            label += f" ({info['code']})"
        if info.get("error"):
            label += f" &mdash; {html.escape(info['error'][:60])}"
        health_path = backend.get("health", "/health")
        cards.append(
            f'<div class="backend-card focusable" data-backend="{html.escape(name)}" '
            f'role="button" tabindex="0" aria-pressed="false" '
            f'aria-controls="backend-endpoint-groups" '
            f'aria-label="View API endpoints for {html.escape(name)}">'
            f'<div class="backend-name">'
            f'<span class="led {html.escape(status)}"></span>'
            f"{html.escape(name)}</div>"
            f'<div class="backend-status">{label}</div>'
            f'<div class="backend-health-url">{html.escape(health_path)}</div>'
            f"</div>"
        )
    return "\n".join(cards)


_TERMINAL_LEVEL_RANK = {
    "DEBUG": 10,
    "INFO": 20,
    "WARNING": 30,
    "ERROR": 40,
    "CRITICAL": 50,
}


def _event_matches_filters(event: dict, *, min_level: str, source_filter: set[str] | None) -> bool:
    if source_filter and event.get("source") not in source_filter:
        return False
    event_level = str(event.get("level", "INFO")).upper()
    return _TERMINAL_LEVEL_RANK.get(event_level, 20) >= _TERMINAL_LEVEL_RANK.get(min_level, 20)


async def _build_dashboard_html() -> str:
    config = get_backends_config()
    backends = config.get("backends", {})
    health_results = {}
    for name, backend in backends.items():
        health_path = backend.get("health", "/health")
        url = f"{backend['url']}{health_path}"
        health_results[name] = await client.health_check(name, url)

    all_healthy = all(r["status"] == "healthy" for r in health_results.values())
    overall = "healthy" if all_healthy else "degraded"
    uptime_secs = _time.time() - _start_time if _start_time else 0

    return (
        _DASHBOARD_TEMPLATE
        .replace("__OVERALL_STATUS_CLASS__", overall)
        .replace("__UPTIME__", _format_uptime(uptime_secs))
        .replace("__UPTIME_SECONDS__", str(int(uptime_secs)))
        .replace("__BACKEND_COUNT__", str(len(backends)))
        .replace("__BACKEND_CARDS__", _build_backend_cards(backends, health_results))
        .replace("__TERMINAL_FEED_MODE__", settings.terminal_feed_mode.strip().lower())
        .replace("__INSTANCE_ID__", html.escape(settings.instance_id))
    )


async def _serve_dashboard() -> HTMLResponse:
    return HTMLResponse(content=await _build_dashboard_html())


@app.get("/events/terminal", include_in_schema=False)
async def terminal_feed_events(request: Request):
    if settings.terminal_feed_mode.strip().lower() != "live":
        raise HTTPException(status_code=404, detail="Terminal feed is disabled (set SYNAPSE_TERMINAL_FEED_MODE=live)")

    feed = get_terminal_feed()
    source_filter = parse_source_filter(request.query_params.get("sources"))
    min_level = validate_level(request.query_params.get("level"), settings.terminal_feed_default_level)
    try:
        backlog = int(request.query_params.get("backlog", str(settings.terminal_feed_backlog_lines)))
    except ValueError:
        backlog = settings.terminal_feed_backlog_lines
    backlog = max(1, min(backlog, 500))
    keepalive_seconds = max(5.0, float(settings.terminal_feed_keepalive_seconds))

    subscriber = feed.subscribe()

    async def event_stream():
        try:
            yield as_sse(
                "meta",
                {
                    "instance": settings.instance_id,
                    "mode": settings.terminal_feed_mode.strip().lower(),
                    "bus_mode": settings.terminal_feed_bus_mode.strip().lower() or "local",
                },
            )
            for event in feed.backlog(limit=backlog, min_level=min_level, allowed_sources=source_filter):
                yield as_sse("log", event)
            while True:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(subscriber.get(), timeout=keepalive_seconds)
                except asyncio.TimeoutError:
                    yield ": keepalive\n\n"
                    continue
                if not _event_matches_filters(event, min_level=min_level, source_filter=source_filter):
                    continue
                yield as_sse("log", event)
        finally:
            feed.unsubscribe(subscriber)

    headers = {
        "Cache-Control": "no-cache, no-transform",
        "Connection": "keep-alive",
        "X-Accel-Buffering": "no",
    }
    return StreamingResponse(event_stream(), media_type="text/event-stream", headers=headers)


@app.get("/dashboard", include_in_schema=False, response_class=HTMLResponse)
async def dashboard():
    """Self-contained HTML status dashboard with live health monitoring."""
    return await _serve_dashboard()


@app.get("/", include_in_schema=False, response_class=HTMLResponse)
async def root_dashboard():
    """Serve dashboard directly on base URL."""
    return await _serve_dashboard()


@app.get("/ui", include_in_schema=False, response_class=HTMLResponse)
async def ui_dashboard():
    """Serve dashboard directly on /ui alias."""
    return await _serve_dashboard()


# --- Mount routers ---

from .router_llm import router as llm_router  # noqa: E402
from .router_tts import router as tts_router  # noqa: E402
from .router_stt import router as stt_router  # noqa: E402
from .router_speaker import router as speaker_router  # noqa: E402
from .router_audio import router as audio_router  # noqa: E402

app.include_router(llm_router)
app.include_router(tts_router)
app.include_router(stt_router)
app.include_router(speaker_router)
app.include_router(audio_router)
