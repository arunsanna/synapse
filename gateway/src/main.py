"""Synapse Gateway — unified AI proxy for ArunLabs Forge cluster."""

import asyncio
import html
import logging
import time as _time
from contextlib import asynccontextmanager

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
            f'<span class="status-dot {html.escape(status)}"></span>'
            f"{html.escape(name)}</div>"
            f'<div class="backend-status">{label}</div>'
            f'<div class="backend-health-url">{html.escape(health_path)}</div>'
            f"</div>"
        )
    return "\n".join(cards)


_DASHBOARD_HTML = """\
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Synapse Gateway</title>
    <style>
        @import url('https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;600;700;800&family=Orbitron:wght@500;700;900&family=Share+Tech+Mono&display=swap');

        :root {
            --background: #0a0a0f;
            --foreground: #e0e0e0;
            --card: #12121a;
            --muted: #1c1c2e;
            --muted-foreground: #8a93a3;
            --accent: #00ff88;
            --accent-secondary: #ff00ff;
            --accent-tertiary: #00d4ff;
            --border: #2a2a3a;
            --input: #12121a;
            --ring: #00ff88;
            --destructive: #ff3366;
            --shadow-neon: 0 0 5px #00ff88, 0 0 10px #00ff8840;
            --shadow-neon-sm: 0 0 3px #00ff88, 0 0 6px #00ff8830;
            --shadow-neon-lg: 0 0 10px #00ff88, 0 0 20px #00ff8860, 0 0 40px #00ff8830;
            --shadow-neon-secondary: 0 0 5px #ff00ff, 0 0 20px #ff00ff60;
            --shadow-neon-tertiary: 0 0 5px #00d4ff, 0 0 20px #00d4ff60;
            --chamfer-md: polygon(
                0 12px, 12px 0,
                calc(100% - 12px) 0, 100% 12px,
                100% calc(100% - 12px), calc(100% - 12px) 100%,
                12px 100%, 0 calc(100% - 12px)
            );
            --chamfer-sm: polygon(
                0 8px, 8px 0,
                calc(100% - 8px) 0, 100% 8px,
                100% calc(100% - 8px), calc(100% - 8px) 100%,
                8px 100%, 0 calc(100% - 8px)
            );
        }

        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }

        html, body { min-height: 100%; }

        body {
            background: var(--background);
            color: var(--foreground);
            font-family: 'JetBrains Mono', 'Fira Code', Consolas, monospace;
            line-height: 1.6;
            letter-spacing: 0.02em;
            padding: 1.25rem;
            position: relative;
            overflow-x: hidden;
        }

        body::before {
            content: "";
            position: fixed;
            inset: 0;
            background-image:
                linear-gradient(rgba(0, 255, 136, 0.03) 1px, transparent 1px),
                linear-gradient(90deg, rgba(0, 255, 136, 0.03) 1px, transparent 1px),
                radial-gradient(circle at 0% 0%, rgba(255, 0, 255, 0.14), transparent 42%),
                radial-gradient(circle at 100% 100%, rgba(0, 212, 255, 0.16), transparent 38%);
            background-size: 50px 50px, 50px 50px, 100% 100%, 100% 100%;
            pointer-events: none;
            z-index: 0;
        }

        body::after {
            content: "";
            position: fixed;
            inset: 0;
            background: repeating-linear-gradient(
                0deg,
                transparent,
                transparent 2px,
                rgba(0, 0, 0, 0.3) 2px,
                rgba(0, 0, 0, 0.3) 4px
            );
            opacity: 0.35;
            pointer-events: none;
            z-index: 3;
        }

        .scanline-sweep {
            position: fixed;
            inset: -100% 0 auto 0;
            height: 24vh;
            background: linear-gradient(
                180deg,
                rgba(0, 255, 136, 0),
                rgba(0, 255, 136, 0.09),
                rgba(0, 255, 136, 0)
            );
            mix-blend-mode: screen;
            animation: scanline 9s linear infinite;
            pointer-events: none;
            z-index: 2;
        }

        a { color: var(--accent-tertiary); text-decoration: none; }
        a:hover { text-decoration: none; }
        code, .mono { font-family: 'Share Tech Mono', 'JetBrains Mono', monospace; }

        .page {
            max-width: none;
            width: 100%;
            margin: 0;
            display: grid;
            gap: 1rem;
            position: relative;
            z-index: 4;
        }

        .cyber-panel {
            background: linear-gradient(140deg, rgba(18, 18, 26, 0.94), rgba(10, 10, 15, 0.88));
            border: 1px solid var(--border);
            clip-path: var(--chamfer-md);
            padding: 1.1rem;
            position: relative;
            overflow: hidden;
            transition: transform 0.15s steps(4), border-color 0.15s steps(4), box-shadow 0.15s steps(4);
        }

        .cyber-panel::before {
            content: "";
            position: absolute;
            inset: 0;
            background: linear-gradient(
                120deg,
                transparent 0%,
                rgba(0, 255, 136, 0.08) 45%,
                rgba(255, 0, 255, 0.08) 60%,
                transparent 100%
            );
            transform: translateX(-120%);
            opacity: 0;
            transition: opacity 0.15s steps(4);
            pointer-events: none;
        }

        .cyber-panel:hover {
            border-color: rgba(0, 255, 136, 0.65);
            box-shadow: var(--shadow-neon-sm);
            transform: translateY(-1px);
        }

        .cyber-panel:hover::before {
            opacity: 0.55;
            animation: panelSweep 1.2s linear;
        }

        /* --- Status Bar (compact header) --- */
        .status-bar {
            display: flex;
            align-items: center;
            gap: 0.8rem;
            padding: 0.6rem 1rem;
            background: linear-gradient(140deg, rgba(18, 18, 26, 0.94), rgba(10, 10, 15, 0.88));
            border: 1px solid var(--border);
            clip-path: var(--chamfer-sm);
            min-height: 44px;
            flex-wrap: wrap;
        }

        .status-bar-title {
            font-family: 'Orbitron', 'Share Tech Mono', monospace;
            font-weight: 800;
            font-size: 1.05rem;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            color: var(--foreground);
            filter: drop-shadow(0 0 6px rgba(0, 255, 136, 0.25));
            white-space: nowrap;
        }

        .status-bar-dot {
            width: 9px;
            height: 9px;
            border-radius: 0;
            display: inline-block;
            transform: rotate(45deg);
            border: 1px solid rgba(255, 255, 255, 0.25);
            flex-shrink: 0;
        }

        .status-bar-dot.healthy {
            background: var(--accent);
            box-shadow: var(--shadow-neon-sm);
        }

        .status-bar-dot.degraded {
            background: #ffb020;
            box-shadow: 0 0 5px #ffb020, 0 0 10px #ffb02055;
        }

        .status-bar-dot.stale {
            background: #5f6f8d;
        }

        .status-bar-sep {
            width: 1px;
            height: 18px;
            background: var(--border);
            flex-shrink: 0;
        }

        .status-bar-chip {
            display: inline-flex;
            align-items: center;
            gap: 0.35rem;
            color: #8a93a3;
            font-size: 0.68rem;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            white-space: nowrap;
        }

        .status-bar-chip code {
            color: var(--accent);
            font-size: 0.72rem;
            letter-spacing: 0.06em;
        }

        /* --- Terminal Section (full-width, collapsible) --- */
        .terminal-section {
            background: rgba(10, 10, 15, 0.92);
            border: 1px solid rgba(0, 212, 255, 0.34);
            clip-path: var(--chamfer-md);
            box-shadow: var(--shadow-neon-tertiary);
            overflow: hidden;
        }

        .terminal-toolbar {
            display: flex;
            align-items: center;
            gap: 0.4rem;
            padding: 0.45rem 0.7rem;
            border-bottom: 1px solid var(--border);
            background: rgba(28, 28, 46, 0.62);
            text-transform: uppercase;
            letter-spacing: 0.14em;
            font-size: 0.67rem;
            color: var(--muted-foreground);
            flex-wrap: wrap;
        }

        .terminal-toolbar-label {
            font-weight: 600;
            margin-right: 0.3rem;
            white-space: nowrap;
        }

        .terminal-filters {
            display: flex;
            gap: 0.25rem;
            align-items: center;
        }

        .terminal-filter-btn {
            background: rgba(42, 42, 58, 0.6);
            border: 1px solid var(--border);
            color: #6b7a8e;
            padding: 0.18rem 0.45rem;
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.62rem;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            cursor: pointer;
            transition: all 0.1s;
            clip-path: var(--chamfer-sm);
        }

        .terminal-filter-btn.active {
            color: #d2e8ff;
            border-color: rgba(0, 212, 255, 0.5);
            background: rgba(0, 212, 255, 0.12);
        }

        .terminal-filter-btn.active.level-warning {
            border-color: rgba(255, 176, 32, 0.5);
            background: rgba(255, 176, 32, 0.12);
            color: #ffd27b;
        }

        .terminal-filter-btn.active.level-error {
            border-color: rgba(255, 51, 102, 0.5);
            background: rgba(255, 51, 102, 0.12);
            color: #ff90a8;
        }

        .terminal-filter-btn.active.level-critical {
            border-color: rgba(255, 51, 102, 0.7);
            background: rgba(255, 51, 102, 0.18);
            color: #ff6088;
        }

        .terminal-conn {
            margin-left: auto;
            font-size: 0.62rem;
            letter-spacing: 0.08em;
            font-family: 'Share Tech Mono', monospace;
        }

        .terminal-conn.live {
            color: var(--accent);
            text-shadow: 0 0 6px rgba(0, 255, 136, 0.45);
        }

        .terminal-conn.connecting {
            color: #ffd27b;
        }

        .terminal-conn.stale {
            color: #ff90a8;
        }

        .terminal-toggle {
            background: rgba(42, 42, 58, 0.6);
            border: 1px solid var(--border);
            color: var(--accent-tertiary);
            padding: 0.18rem 0.5rem;
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.62rem;
            letter-spacing: 0.06em;
            cursor: pointer;
            clip-path: var(--chamfer-sm);
            transition: border-color 0.1s;
            margin-left: 0.3rem;
        }

        .terminal-toggle:hover {
            border-color: var(--accent-tertiary);
        }

        .terminal-body {
            padding: 0.6rem 0.8rem;
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.78rem;
            color: #d2e8ff;
            display: flex;
            flex-direction: column;
            gap: 0.28rem;
            overflow-y: auto;
            height: 120px;
            transition: height 0.25s ease;
        }

        .terminal-body.expanded {
            height: 50vh;
        }

        .term-line {
            display: grid;
            grid-template-columns: 60px 180px minmax(0, 1fr);
            gap: 0.45rem;
            min-height: 20px;
            align-items: start;
            border-bottom: 1px solid rgba(42, 42, 58, 0.45);
            padding-bottom: 0.24rem;
            word-break: break-word;
        }

        .term-line.system {
            color: #8ea3ba;
        }

        .term-level {
            color: #8fe6ff;
        }

        .term-level.warning {
            color: #ffd27b;
        }

        .term-level.error,
        .term-level.critical {
            color: #ff90a8;
        }

        .term-src {
            color: #9cb2cb;
        }

        .term-msg {
            color: #d9e4f1;
        }

        .dashboard-grid {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 1rem;
        }

        .panel {
            background: linear-gradient(180deg, rgba(18, 18, 26, 0.95), rgba(10, 10, 15, 0.92));
            border: 1px solid var(--border);
            clip-path: var(--chamfer-md);
            padding: 1rem;
            position: relative;
            overflow: hidden;
            transition: all 0.15s steps(4);
        }

        .panel::after {
            content: "";
            position: absolute;
            top: 0;
            left: 0;
            width: 34%;
            height: 1px;
            background: linear-gradient(90deg, var(--accent), transparent);
            opacity: 0.95;
        }

        .panel:hover {
            border-color: rgba(0, 255, 136, 0.58);
            box-shadow: var(--shadow-neon-sm);
        }

        .panel-title {
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.76rem;
            font-weight: 700;
            color: var(--accent-tertiary);
            text-transform: uppercase;
            letter-spacing: 0.2em;
            margin-bottom: 0.78rem;
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.6rem;
        }

        .panel-title .refresh-info {
            font-size: 0.67rem;
            font-weight: 400;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--muted-foreground);
        }

        .table-wrap {
            overflow-x: auto;
            border: 1px solid rgba(42, 42, 58, 0.85);
            clip-path: var(--chamfer-sm);
            background: rgba(18, 18, 26, 0.6);
        }

        table {
            width: 100%;
            border-collapse: collapse;
            font-size: 0.82rem;
        }

        th {
            text-align: left;
            padding: 0.65rem 0.75rem;
            background: rgba(0, 0, 0, 0.42);
            color: #86a6b8;
            font-weight: 600;
            font-family: 'Share Tech Mono', monospace;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            border-bottom: 1px solid var(--border);
            white-space: nowrap;
        }

        td {
            padding: 0.65rem 0.75rem;
            border-bottom: 1px solid rgba(42, 42, 58, 0.65);
            vertical-align: top;
        }

        tr:hover td { background: rgba(0, 212, 255, 0.05); }
        td code { color: var(--accent-tertiary); font-size: 0.78rem; }

        .backends-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(230px, 1fr));
            gap: 0.72rem;
            transform: skewY(-1deg);
        }

        .backend-card {
            background: rgba(12, 12, 20, 0.9);
            border: 1px solid var(--border);
            clip-path: var(--chamfer-sm);
            padding: 0.78rem;
            transform: skewY(1deg);
            transition: all 0.15s steps(4);
            cursor: pointer;
        }

        .backend-card:hover {
            border-color: rgba(0, 255, 136, 0.72);
            box-shadow: var(--shadow-neon-sm);
        }

        .backend-card.active {
            border-color: rgba(0, 212, 255, 0.78);
            box-shadow: var(--shadow-neon-tertiary);
            background: rgba(8, 20, 30, 0.82);
        }

        .backend-name {
            font-weight: 700;
            font-size: 0.83rem;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            display: flex;
            align-items: center;
            gap: 0.5rem;
        }

        .status-dot {
            width: 8px;
            height: 8px;
            border-radius: 0;
            transform: rotate(45deg);
            display: inline-block;
            flex-shrink: 0;
            border: 1px solid rgba(255, 255, 255, 0.2);
        }

        .status-dot.healthy { background: var(--accent); box-shadow: var(--shadow-neon-sm); }
        .status-dot.unhealthy,
        .status-dot.unreachable { background: var(--destructive); box-shadow: 0 0 5px #ff3366, 0 0 10px #ff336640; }
        .status-dot.degraded { background: #ffb020; box-shadow: 0 0 5px #ffb020, 0 0 10px #ffb02040; }
        .status-dot.checking { background: #5f6f8d; }
        .status-dot.stale { background: #5f6f8d; box-shadow: 0 0 4px #5f6f8d, 0 0 8px #5f6f8d40; }

        .backend-status {
            font-size: 0.73rem;
            color: #9fb1be;
            margin-top: 0.25rem;
            min-height: 2.35em;
        }

        .backend-health-url {
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.7rem;
            color: #7183a0;
            margin-top: 0.25rem;
            letter-spacing: 0.08em;
            text-transform: uppercase;
        }

        .model-toolbar {
            display: flex;
            align-items: center;
            gap: 0.65rem;
            flex-wrap: wrap;
            margin-bottom: 0.72rem;
        }

        .model-load-defaults {
            display: flex;
            align-items: flex-end;
            gap: 0.55rem;
            flex-wrap: wrap;
        }

        .load-default-field {
            display: flex;
            flex-direction: column;
            gap: 0.18rem;
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.62rem;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #89a0b8;
        }

        .load-default-input {
            min-height: 34px;
            border: 1px solid rgba(0, 212, 255, 0.45);
            background: rgba(0, 212, 255, 0.08);
            color: #d5f7ff;
            padding: 0.22rem 0.42rem;
            clip-path: var(--chamfer-sm);
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.68rem;
            letter-spacing: 0.04em;
            width: 88px;
        }

        .load-default-input.prompt {
            width: min(540px, 72vw);
        }

        .model-action-status {
            font-size: 0.74rem;
            color: #8aa2b7;
            font-family: 'Share Tech Mono', monospace;
            text-transform: uppercase;
            letter-spacing: 0.08em;
        }

        .model-action-status.error { color: #ff7892; }

        .btn {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 42px;
            border: 2px solid var(--accent);
            clip-path: var(--chamfer-sm);
            padding: 0.38rem 0.75rem;
            background: transparent;
            color: var(--accent);
            font-family: 'Share Tech Mono', monospace;
            text-transform: uppercase;
            letter-spacing: 0.16em;
            font-size: 0.68rem;
            cursor: pointer;
            transition: all 0.1s steps(4);
            text-shadow: 0 0 8px rgba(0, 255, 136, 0.4);
        }

        .btn:hover:enabled {
            background: var(--accent);
            color: #06120c;
            box-shadow: var(--shadow-neon);
        }

        .btn:disabled {
            opacity: 0.45;
            cursor: not-allowed;
            box-shadow: none;
        }

        .btn.load { border-color: var(--accent); color: var(--accent); }
        .btn.unload { border-color: var(--accent-secondary); color: var(--accent-secondary); text-shadow: 0 0 8px rgba(255, 0, 255, 0.4); }
        .btn.unload:hover:enabled {
            background: var(--accent-secondary);
            color: #170418;
            box-shadow: var(--shadow-neon-secondary);
        }

        .status-pill {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 42px;
            min-width: 92px;
            padding: 0.2rem 0.72rem;
            font-size: 0.68rem;
            font-family: 'Share Tech Mono', monospace;
            text-transform: uppercase;
            letter-spacing: 0.11em;
            border: 1px solid transparent;
            clip-path: var(--chamfer-sm);
        }

        .status-pill.loaded { color: #7fffbc; border-color: #1f6645; background: rgba(0, 255, 136, 0.14); }
        .status-pill.loading { color: #ffd27b; border-color: #9f6109; background: rgba(255, 176, 32, 0.14); }
        .status-pill.unloading { color: #ffd27b; border-color: #9f6109; background: rgba(255, 176, 32, 0.14); }
        .status-pill.unloaded { color: #b4c0cd; border-color: #465169; background: rgba(28, 28, 46, 0.65); }
        .status-pill.failed { color: #ff8fa6; border-color: #942848; background: rgba(255, 51, 102, 0.14); }
        .status-pill.unknown { color: #8adaff; border-color: #16739d; background: rgba(0, 212, 255, 0.14); }

        .model-actions {
            display: inline-flex;
            gap: 0.45rem;
            flex-wrap: nowrap;
            white-space: nowrap;
        }

        .model-link {
            border: 1px solid rgba(0, 212, 255, 0.35);
            background: rgba(0, 212, 255, 0.08);
            color: #8fe6ff;
            clip-path: var(--chamfer-sm);
            padding: 0.32rem 0.55rem;
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.72rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            cursor: pointer;
            transition: all 0.1s steps(4);
        }

        .model-link:hover {
            border-color: var(--accent-tertiary);
            box-shadow: var(--shadow-neon-tertiary);
            background: rgba(0, 212, 255, 0.16);
        }

        .device-badge {
            display: inline-flex;
            align-items: center;
            min-height: 24px;
            padding: 0.1rem 0.52rem;
            border: 1px solid rgba(0, 255, 136, 0.45);
            color: var(--accent);
            background: rgba(0, 255, 136, 0.09);
            font-size: 0.67rem;
            font-family: 'Share Tech Mono', monospace;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            clip-path: var(--chamfer-sm);
        }

        .device-badge.gpu {
            border-color: rgba(255, 0, 255, 0.45);
            color: #ff9eff;
            background: rgba(255, 0, 255, 0.1);
        }

        .endpoint-grid {
            display: grid;
            grid-template-columns: minmax(0, 1fr) 280px;
            gap: 1rem;
            align-items: start;
        }

        .backend-endpoints-hud {
            margin-top: 0.9rem;
            padding: 0.9rem;
        }

        .endpoint-selected-backend {
            font-size: 0.72rem;
            font-family: 'Share Tech Mono', monospace;
            text-transform: uppercase;
            letter-spacing: 0.14em;
            color: #8ad8f8;
            margin-bottom: 0.6rem;
        }

        .endpoint-shell {
            position: relative;
            background: rgba(10, 10, 15, 0.88);
            border: 1px solid rgba(42, 42, 58, 0.95);
            clip-path: var(--chamfer-md);
            padding: 0.95rem;
        }

        .endpoint-shell::before {
            content: "";
            position: absolute;
            top: 0;
            right: 0;
            width: 46%;
            height: 1px;
            background: linear-gradient(90deg, transparent, var(--accent-secondary));
        }

        .endpoint-group { margin-bottom: 1rem; }
        .endpoint-group:last-child { margin-bottom: 0; }

        .endpoint-empty {
            color: #8aa2b7;
            font-size: 0.76rem;
            text-transform: uppercase;
            letter-spacing: 0.1em;
            padding: 0.4rem 0;
        }

        .endpoint-group-title {
            font-size: 0.68rem;
            font-family: 'Share Tech Mono', monospace;
            font-weight: 600;
            color: #8296ad;
            text-transform: uppercase;
            letter-spacing: 0.18em;
            margin-bottom: 0.33rem;
            padding-bottom: 0.25rem;
            border-bottom: 1px solid rgba(42, 42, 58, 0.9);
        }

        .endpoint-row {
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.78rem;
            padding: 0.22rem 0;
            display: flex;
            gap: 0.7rem;
        }

        .ep-method {
            font-weight: 700;
            min-width: 3.5rem;
            text-align: right;
            letter-spacing: 0.08em;
        }

        .ep-method.get { color: var(--accent); text-shadow: 0 0 7px rgba(0, 255, 136, 0.45); }
        .ep-method.post { color: var(--accent-tertiary); text-shadow: 0 0 7px rgba(0, 212, 255, 0.45); }
        .ep-method.delete { color: var(--destructive); text-shadow: 0 0 7px rgba(255, 51, 102, 0.45); }
        .ep-path { color: #d0dbe8; }

        .quick-links {
            display: grid;
            gap: 0.55rem;
            align-content: start;
            transform: rotate(1deg);
        }

        .quick-links.inline {
            transform: none;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
        }

        .quick-links a {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 44px;
            padding: 0.58rem 0.75rem;
            border: 1px solid var(--border);
            clip-path: var(--chamfer-sm);
            background: rgba(18, 18, 26, 0.88);
            color: #c3d0df;
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.74rem;
            text-transform: uppercase;
            letter-spacing: 0.14em;
            transition: all 0.1s steps(4);
        }

        .quick-links a:hover {
            border-color: var(--accent-tertiary);
            color: var(--accent-tertiary);
            box-shadow: var(--shadow-neon-tertiary);
            transform: translateX(2px);
        }

        .focusable:focus-visible,
        button:focus-visible,
        a:focus-visible {
            outline: none;
            box-shadow: 0 0 0 2px var(--ring), 0 0 0 5px rgba(10, 10, 15, 1), var(--shadow-neon);
        }

        .sr-only {
            position: absolute;
            width: 1px;
            height: 1px;
            padding: 0;
            margin: -1px;
            overflow: hidden;
            clip: rect(0, 0, 0, 0);
            border: 0;
        }

        .model-modal {
            position: fixed;
            inset: 0;
            z-index: 30;
            background: rgba(4, 6, 12, 0.78);
            backdrop-filter: blur(2px);
            display: none;
            align-items: center;
            justify-content: center;
            padding: 1rem;
        }

        .model-modal.open {
            display: flex;
        }

        .model-modal-panel {
            width: min(980px, 100%);
            max-height: 88vh;
            overflow: auto;
            background: linear-gradient(180deg, rgba(11, 14, 23, 0.98), rgba(10, 10, 15, 0.98));
            border: 1px solid rgba(0, 212, 255, 0.45);
            box-shadow: var(--shadow-neon-tertiary);
            clip-path: var(--chamfer-md);
            padding: 1rem;
        }

        .model-modal-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.8rem;
            margin-bottom: 0.7rem;
        }

        .model-modal-title {
            font-family: 'Orbitron', 'Share Tech Mono', monospace;
            font-size: 1rem;
            letter-spacing: 0.12em;
            text-transform: uppercase;
            color: #d4eeff;
        }

        .model-modal-live {
            margin-bottom: 0.7rem;
            font-size: 0.62rem;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            color: #8ea3ba;
            font-family: 'Share Tech Mono', monospace;
        }

        .model-modal-live.stale {
            color: #ffadad;
        }

        .model-meta-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 0.55rem;
            margin-bottom: 0.8rem;
        }

        .model-meta-item {
            border: 1px solid rgba(42, 42, 58, 0.9);
            background: rgba(12, 14, 22, 0.8);
            clip-path: var(--chamfer-sm);
            padding: 0.48rem 0.58rem;
        }

        .model-meta-label {
            font-size: 0.62rem;
            letter-spacing: 0.14em;
            text-transform: uppercase;
            color: #7f93ac;
            margin-bottom: 0.2rem;
            font-family: 'Share Tech Mono', monospace;
        }

        .model-meta-value {
            font-size: 0.77rem;
            color: #d9e4f1;
            word-break: break-word;
        }

        .model-arg-grid {
            margin-top: 0.25rem;
            margin-bottom: 0.85rem;
            border: 1px solid rgba(42, 42, 58, 0.88);
            clip-path: var(--chamfer-sm);
            overflow: hidden;
        }

        .model-arg-row {
            display: grid;
            grid-template-columns: 220px minmax(0, 1fr);
            gap: 0.7rem;
            padding: 0.42rem 0.65rem;
            border-bottom: 1px solid rgba(42, 42, 58, 0.68);
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.74rem;
        }

        .model-arg-row:last-child {
            border-bottom: 0;
        }

        .model-arg-key {
            color: #8fe6ff;
            text-transform: lowercase;
            word-break: break-all;
        }

        .model-arg-value {
            color: #c6d5e5;
            word-break: break-word;
        }

        .model-arg-help {
            margin-top: 0.25rem;
            font-size: 0.66rem;
            color: #86a3bc;
            letter-spacing: 0.05em;
        }

        .model-profile-shell {
            border: 1px solid rgba(42, 42, 58, 0.88);
            background: rgba(12, 14, 22, 0.75);
            clip-path: var(--chamfer-sm);
            padding: 0.7rem;
            margin-bottom: 0.85rem;
        }

        .model-source-shell {
            border: 1px solid rgba(42, 42, 58, 0.88);
            background: rgba(12, 14, 22, 0.75);
            clip-path: var(--chamfer-sm);
            padding: 0.7rem;
            margin-bottom: 0.85rem;
        }

        .model-source-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));
            gap: 0.55rem;
        }

        .model-source-card {
            border: 1px solid rgba(42, 42, 58, 0.82);
            background: rgba(9, 11, 18, 0.75);
            clip-path: var(--chamfer-sm);
            padding: 0.5rem 0.6rem;
        }

        .model-source-title {
            font-size: 0.64rem;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: #8fe6ff;
            margin-bottom: 0.34rem;
            font-family: 'Share Tech Mono', monospace;
        }

        .model-source-row {
            display: flex;
            justify-content: space-between;
            gap: 0.7rem;
            padding: 0.12rem 0;
            border-bottom: 1px solid rgba(42, 42, 58, 0.55);
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.68rem;
        }

        .model-source-row:last-child {
            border-bottom: 0;
        }

        .model-source-key {
            color: #90aec9;
            text-transform: lowercase;
            word-break: break-word;
        }

        .model-source-value {
            color: #d8f2ff;
            text-align: right;
            word-break: break-word;
        }

        .model-source-meta {
            margin-top: 0.35rem;
            font-size: 0.62rem;
            letter-spacing: 0.06em;
            color: #86a3bc;
            font-family: 'Share Tech Mono', monospace;
        }

        .model-source-link {
            color: #79d7ff;
            text-decoration: none;
            border-bottom: 1px dashed rgba(121, 215, 255, 0.4);
        }

        .model-source-link:hover {
            color: #bff0ff;
            border-bottom-color: rgba(191, 240, 255, 0.7);
        }

        .model-profile-grid {
            display: grid;
            gap: 0.7rem;
        }

        .model-profile-row {
            border: 1px solid rgba(42, 42, 58, 0.82);
            background: rgba(9, 11, 18, 0.75);
            clip-path: var(--chamfer-sm);
            padding: 0.55rem 0.65rem;
        }

        .model-profile-topline {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.6rem;
            margin-bottom: 0.34rem;
        }

        .model-profile-name {
            font-size: 0.68rem;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: #9dd8ff;
            font-family: 'Share Tech Mono', monospace;
        }

        .model-profile-scope {
            font-size: 0.62rem;
            text-transform: uppercase;
            letter-spacing: 0.12em;
            color: #87a0b7;
            border: 1px solid rgba(42, 42, 58, 0.9);
            padding: 0.1rem 0.35rem;
            clip-path: var(--chamfer-sm);
        }

        .model-profile-input,
        .model-profile-select,
        .model-profile-textarea {
            width: 100%;
            border: 1px solid rgba(0, 212, 255, 0.4);
            background: rgba(0, 212, 255, 0.08);
            color: #d8f6ff;
            padding: 0.34rem 0.45rem;
            clip-path: var(--chamfer-sm);
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.72rem;
            letter-spacing: 0.04em;
        }

        .model-profile-textarea {
            min-height: 82px;
            resize: vertical;
        }

        .model-profile-range-wrap {
            display: grid;
            gap: 0.36rem;
        }

        .model-profile-slider {
            width: 100%;
            accent-color: #00d4ff;
            cursor: pointer;
        }

        .model-profile-range-meta {
            display: grid;
            grid-template-columns: 1fr auto 1fr;
            gap: 0.5rem;
            align-items: center;
            font-family: 'Share Tech Mono', monospace;
            font-size: 0.62rem;
            letter-spacing: 0.06em;
            color: #8ba8c1;
        }

        .model-profile-range-meta span:last-child {
            text-align: right;
        }

        .model-profile-range-value {
            color: #98f2d2;
            text-align: center;
        }

        .model-profile-help {
            margin-top: 0.36rem;
            font-size: 0.66rem;
            color: #8da7be;
            letter-spacing: 0.05em;
        }

        .model-profile-actions {
            display: flex;
            flex-wrap: wrap;
            gap: 0.5rem;
            align-items: center;
            margin-top: 0.75rem;
        }

        .model-profile-status {
            font-size: 0.7rem;
            color: #8fd8a9;
            letter-spacing: 0.08em;
            text-transform: uppercase;
            font-family: 'Share Tech Mono', monospace;
        }

        .model-profile-status.error {
            color: #ff8099;
        }

        .model-profile-notes {
            margin-top: 0.7rem;
            border-top: 1px solid rgba(42, 42, 58, 0.82);
            padding-top: 0.55rem;
            display: grid;
            gap: 0.35rem;
        }

        .model-profile-note {
            font-size: 0.66rem;
            color: #87a3ba;
            letter-spacing: 0.05em;
        }

        .model-json-wrap {
            border: 1px solid rgba(42, 42, 58, 0.95);
            clip-path: var(--chamfer-sm);
            background: rgba(10, 10, 15, 0.95);
            overflow: auto;
        }

        .model-json {
            margin: 0;
            padding: 0.75rem;
            color: #9ee8ff;
            font-size: 0.72rem;
            line-height: 1.45;
            font-family: 'Share Tech Mono', monospace;
            white-space: pre;
        }

        @keyframes scanline {
            0% { transform: translateY(-120%); }
            100% { transform: translateY(220vh); }
        }

        @keyframes panelSweep {
            0% { transform: translateX(-120%); }
            100% { transform: translateX(120%); }
        }

        @media (max-width: 1120px) {
            .dashboard-grid { grid-template-columns: 1fr; }
            .endpoint-grid { grid-template-columns: 1fr; }
            .quick-links { transform: none; grid-template-columns: repeat(2, minmax(0, 1fr)); }
            .quick-links.inline { grid-template-columns: repeat(2, minmax(0, 1fr)); }
        }

        /* --- Bottom Sheet (base, hidden on desktop) --- */
        .bottom-sheet-backdrop { display: none; }
        .bottom-sheet { display: none; }
        .mobile-overflow-btn { display: none; }
        .mobile-health-chips { display: none; }
        .mobile-model-bar { display: none; }
        .mobile-filter-btn { display: none; }
        .mobile-filter-dropdown { display: none; }

        @media (max-width: 700px) {
            body {
                padding: 0.85rem;
                padding-left: max(0.85rem, env(safe-area-inset-left));
                padding-right: max(0.85rem, env(safe-area-inset-right));
                padding-bottom: env(safe-area-inset-bottom);
            }
            .panel, .cyber-panel { padding: 0.85rem; }
            .page { gap: 0.65rem; }

            /* --- Status bar compact --- */
            .status-bar { gap: 0.4rem; padding: 0.5rem 0.7rem; }
            .status-bar-title { font-size: 0.85rem; }
            .status-bar-sep { display: none; }
            .chip-baseurl { display: none; }
            .chip-backends { display: none; }
            .mobile-overflow-btn {
                display: flex;
                margin-left: auto;
                background: none;
                border: 1px solid var(--border);
                color: var(--muted-foreground);
                padding: 4px 8px;
                border-radius: 4px;
                cursor: pointer;
                align-items: center;
            }
            .mobile-overflow-btn:active { border-color: var(--accent-tertiary); color: var(--accent-tertiary); }

            /* --- Terminal mobile reformat --- */
            .terminal-toolbar { flex-wrap: wrap; gap: 0.3rem; position: relative; }
            .terminal-filters { display: none; }
            .mobile-filter-btn {
                display: flex;
                background: rgba(42, 42, 58, 0.6);
                border: 1px solid var(--border);
                color: var(--accent-tertiary);
                padding: 4px 6px;
                cursor: pointer;
                clip-path: var(--chamfer-sm);
                align-items: center;
                justify-content: center;
            }
            .mobile-filter-dropdown {
                position: absolute;
                top: 100%;
                right: 0;
                z-index: 15;
                background: rgba(14, 14, 22, 0.98);
                border: 1px solid var(--border);
                border-radius: 6px;
                padding: 0.4rem;
                display: none;
                flex-direction: column;
                gap: 0.25rem;
                box-shadow: 0 4px 12px rgba(0, 0, 0, 0.5);
            }
            .mobile-filter-dropdown.open { display: flex; }
            .mobile-filter-dropdown .terminal-filter-btn { width: 100%; justify-content: flex-start; }
            .term-line {
                display: flex !important;
                flex-wrap: wrap;
                gap: 0.2rem 0.4rem;
            }
            .term-level { flex-shrink: 0; }
            .term-src { flex-shrink: 0; max-width: 120px; overflow: hidden; text-overflow: ellipsis; }
            .term-msg { flex-basis: 100%; word-break: break-word; }
            .terminal-body { height: 200px; }
            .terminal-body.expanded { height: calc(100vh - 160px); }

            /* --- Health chips row --- */
            .mobile-health-chips {
                display: flex;
                gap: 0.45rem;
                overflow-x: auto;
                -webkit-overflow-scrolling: touch;
                scrollbar-width: none;
                padding: 0.3rem 0;
                scroll-snap-type: x proximity;
            }
            .mobile-health-chips::-webkit-scrollbar { display: none; }
            .mobile-health-chip {
                display: inline-flex;
                align-items: center;
                gap: 0.35rem;
                padding: 0.3rem 0.65rem;
                background: rgba(18, 18, 26, 0.9);
                border: 1px solid var(--border);
                border-radius: 20px;
                font-family: 'Share Tech Mono', monospace;
                font-size: 0.65rem;
                font-weight: 600;
                letter-spacing: 0.08em;
                text-transform: uppercase;
                color: #b4c0cd;
                white-space: nowrap;
                cursor: pointer;
                flex-shrink: 0;
                scroll-snap-align: start;
                transition: border-color 0.15s;
            }
            .mobile-health-chip:active { border-color: var(--accent-tertiary); }
            .chip-dot {
                width: 6px; height: 6px;
                border-radius: 0;
                transform: rotate(45deg);
                display: inline-block;
                flex-shrink: 0;
            }
            .chip-dot.healthy { background: var(--accent); box-shadow: var(--shadow-neon-sm); }
            .chip-dot.unhealthy, .chip-dot.unreachable { background: var(--destructive); }
            .chip-dot.degraded { background: #ffb020; }
            .chip-dot.checking, .chip-dot.stale { background: #5f6f8d; }

            /* --- Model quick-action bar --- */
            .mobile-model-bar {
                display: flex;
                align-items: center;
                gap: 0.5rem;
                padding: 0.55rem 0.85rem;
                background: linear-gradient(140deg, rgba(18, 18, 26, 0.94), rgba(10, 10, 15, 0.88));
                border: 1px solid var(--border);
                clip-path: var(--chamfer-sm);
                cursor: pointer;
            }
            .mobile-model-bar:active { border-color: var(--accent-tertiary); }
            .mobile-model-bar-label {
                font-family: 'Share Tech Mono', monospace;
                font-size: 0.65rem;
                color: var(--accent-tertiary);
                text-transform: uppercase;
                letter-spacing: 0.15em;
            }
            .mobile-model-name {
                flex: 1;
                font-size: 0.75rem;
                font-weight: 600;
                color: #d4eeff;
                overflow: hidden;
                text-overflow: ellipsis;
                white-space: nowrap;
            }
            .mobile-model-status .status-pill { font-size: 0.6rem; padding: 0.15rem 0.45rem; min-height: 0; }
            .mobile-model-chevron { color: var(--muted-foreground); flex-shrink: 0; }

            /* --- Hide desktop panels, show mobile elements --- */
            .dashboard-grid { display: none; }
            .page > section.panel:last-of-type { display: none; }

            /* --- Bottom sheet active on mobile --- */
            .bottom-sheet-backdrop {
                display: block;
                position: fixed;
                inset: 0;
                z-index: 40;
                background: rgba(4, 6, 12, 0.65);
                backdrop-filter: blur(2px);
                opacity: 0;
                pointer-events: none;
                transition: opacity 0.25s ease;
            }
            .bottom-sheet-backdrop.active { opacity: 1; pointer-events: auto; }
            .bottom-sheet {
                display: block;
                position: fixed;
                bottom: 0; left: 0; right: 0;
                z-index: 41;
                max-height: 85vh;
                background: linear-gradient(180deg, rgba(14, 14, 22, 0.99), rgba(10, 10, 15, 0.99));
                border-top: 1px solid rgba(0, 212, 255, 0.45);
                box-shadow: 0 -4px 20px rgba(0, 0, 0, 0.6), 0 -1px 8px rgba(0, 212, 255, 0.15);
                border-radius: 14px 14px 0 0;
                transform: translateY(100%);
                transition: transform 0.3s cubic-bezier(0.32, 0.72, 0, 1);
                overflow-y: auto;
                -webkit-overflow-scrolling: touch;
                overscroll-behavior: contain;
                padding-bottom: env(safe-area-inset-bottom, 0px);
            }
            .bottom-sheet.open { transform: translateY(0); }
            .bs-handle {
                display: flex;
                justify-content: center;
                padding: 10px 0 6px;
                cursor: grab;
                position: sticky;
                top: 0; z-index: 1;
                background: inherit;
            }
            .bs-handle::after {
                content: "";
                width: 36px; height: 4px;
                border-radius: 2px;
                background: rgba(255, 255, 255, 0.25);
            }
            .bs-content { padding: 0 1rem 1rem; }
            .bs-title {
                font-family: 'Share Tech Mono', monospace;
                font-size: 0.72rem;
                font-weight: 700;
                color: var(--accent-tertiary);
                text-transform: uppercase;
                letter-spacing: 0.18em;
                margin-bottom: 0.6rem;
                padding-top: 0.2rem;
            }
            .bs-content .backend-card { clip-path: none; cursor: default; }
            .bs-content .backends-grid { grid-template-columns: 1fr; transform: none; }
            .bs-content .model-toolbar { flex-direction: column; align-items: flex-start; }
            .bs-content .model-meta-grid { grid-template-columns: 1fr; }
            .bs-content .model-arg-row { grid-template-columns: 1fr; }
            .bs-content .quick-links { grid-template-columns: 1fr; }
            .bs-content .quick-links.inline { grid-template-columns: 1fr; }
            .bs-content .table-wrap { overflow-x: auto; }
            .bs-content table { font-size: 0.72rem; }

            /* --- Model modal as bottom sheet on mobile --- */
            .model-modal { align-items: flex-end; padding: 0; }
            .model-modal-panel {
                width: 100%;
                max-height: 90vh;
                border-radius: 14px 14px 0 0;
                clip-path: none;
                border: none;
                border-top: 1px solid rgba(0, 212, 255, 0.45);
                padding-bottom: env(safe-area-inset-bottom, 0px);
            }
            .model-modal-header {
                position: sticky; top: 0;
                background: rgba(11, 14, 23, 0.98);
                z-index: 1;
                padding-top: 0.75rem;
            }
            .model-modal-title { font-size: 0.82rem; }
        }

        @media (prefers-reduced-motion: reduce) {
            .scanline-sweep {
                animation: none !important;
            }

            .cyber-panel:hover,
            .cyber-panel:hover::before,
            .panel:hover,
            .backend-card:hover,
            .quick-links a:hover {
                transform: none;
                animation: none !important;
            }

            .terminal-body {
                transition: none;
            }
        }
    </style>
</head>
<body>
    <div class="scanline-sweep"></div>
    <main class="page">
        <header class="status-bar">
            <span class="status-bar-title">Synapse Gateway</span>
            <span id="overall-dot" class="status-bar-dot __OVERALL_STATUS_CLASS__"></span>
            <span class="status-bar-sep"></span>
            <span class="status-bar-chip chip-baseurl">Base URL <code>synapse.arunlabs.com</code></span>
            <span class="status-bar-chip chip-uptime">Uptime <code id="uptime">__UPTIME__</code></span>
            <span class="status-bar-chip chip-backends">Backends <code>__BACKEND_COUNT__</code></span>
            <button id="mobile-overflow-btn" class="mobile-overflow-btn" type="button" aria-label="More options">
                <svg width="18" height="18" viewBox="0 0 18 18" fill="none"><circle cx="3" cy="9" r="1.5" fill="currentColor"/><circle cx="9" cy="9" r="1.5" fill="currentColor"/><circle cx="15" cy="9" r="1.5" fill="currentColor"/></svg>
            </button>
        </header>

        <section class="terminal-section">
            <div class="terminal-toolbar">
                <span class="terminal-toolbar-label">Terminal Feed</span>
                <div class="terminal-filters">
                    <button class="terminal-filter-btn active" data-level="INFO">Info</button>
                    <button class="terminal-filter-btn active level-warning" data-level="WARNING">Warn</button>
                    <button class="terminal-filter-btn active level-error" data-level="ERROR">Error</button>
                    <button class="terminal-filter-btn active level-critical" data-level="CRITICAL">Crit</button>
                </div>
                <button id="mobile-filter-btn" class="mobile-filter-btn" type="button" aria-label="Filter log levels" aria-expanded="false">
                    <svg width="16" height="16" viewBox="0 0 16 16" fill="none"><path d="M1 2h14L10 8.5V13l-4 2V8.5L1 2z" stroke="currentColor" stroke-width="1.2" stroke-linejoin="round"/></svg>
                </button>
                <div id="mobile-filter-dropdown" class="mobile-filter-dropdown" role="menu">
                    <button class="terminal-filter-btn active" data-level="INFO">Info</button>
                    <button class="terminal-filter-btn active level-warning" data-level="WARNING">Warn</button>
                    <button class="terminal-filter-btn active level-error" data-level="ERROR">Error</button>
                    <button class="terminal-filter-btn active level-critical" data-level="CRITICAL">Crit</button>
                </div>
                <span id="terminal-conn" class="terminal-conn connecting">connecting</span>
                <button id="terminal-toggle" class="terminal-toggle">Expand</button>
            </div>
            <div id="terminal-feed" class="terminal-body" role="log" aria-live="polite" aria-relevant="additions text">
                <div class="term-line system"><span class="term-level">INFO</span><span class="term-src">gateway.bootstrap</span><span class="term-msg">terminal feed initializing...</span></div>
            </div>
        </section>

        <nav id="mobile-health-chips" class="mobile-health-chips" aria-label="Backend health status"></nav>

        <div id="mobile-model-bar" class="mobile-model-bar" role="button" tabindex="0" aria-label="Model status, tap for controls">
            <span class="mobile-model-bar-label">Model</span>
            <span id="mobile-model-name" class="mobile-model-name">No model loaded</span>
            <span id="mobile-model-status" class="mobile-model-status"><span class="status-pill unknown">checking</span></span>
            <svg class="mobile-model-chevron" width="14" height="14" viewBox="0 0 14 14" fill="none"><path d="M4 5l3 3 3-3" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
        </div>

        <section class="dashboard-grid">
            <div class="panel">
                <div class="panel-title">
                    Backend Health
                    <span class="refresh-info" id="refresh-info" aria-live="polite">Auto-refreshes every 30s</span>
                </div>
                <div class="backends-grid" id="backends-grid">__BACKEND_CARDS__</div>
                <div class="endpoint-shell backend-endpoints-hud">
                    <div class="panel-title">
                        Backend API Inspector
                        <span id="endpoint-total" style="font-weight:400;font-size:0.67rem;color:#7f91a8">0 total</span>
                    </div>
                    <div id="endpoint-selected-backend" class="endpoint-selected-backend">Select a backend node</div>
                    <div id="backend-endpoint-groups" aria-live="polite"></div>
                </div>
            </div>

            <div class="panel">
                <div class="panel-title">
                    LLM Model Control
                    <span class="refresh-info" id="models-refresh-info" aria-live="polite">Auto-refreshes every 10s</span>
                </div>
                <div class="model-toolbar">
                    <button id="refresh-models-btn" class="btn focusable">Refresh Models</button>
                    <span id="model-action-status" class="model-action-status" aria-live="polite">No model actions yet.</span>
                </div>
                <div class="table-wrap">
                    <table>
                        <thead>
                            <tr>
                                <th>Model</th><th>Status</th><th>Actions</th>
                            </tr>
                        </thead>
                        <tbody id="models-table-body">
                            <tr>
                                <td colspan="3" style="color:#8ea2b7">Loading model registry...</td>
                            </tr>
                        </tbody>
                    </table>
                </div>
            </div>
        </section>

        <section class="panel">
            <div class="panel-title">Operator Links</div>
            <div class="quick-links inline">
                <a class="focusable" href="/docs">Swagger UI</a>
                <a class="focusable" href="/redoc">ReDoc</a>
                <a class="focusable" href="/openapi.json">OpenAPI Spec</a>
                <a class="focusable" href="/health">Health JSON</a>
            </div>
        </section>
    </main>

    <div id="bs-backdrop" class="bottom-sheet-backdrop" aria-hidden="true"></div>
    <div id="bs-health" class="bottom-sheet" aria-hidden="true" role="dialog" aria-label="Backend health details">
        <div class="bs-handle" data-bs-handle></div>
        <div class="bs-content">
            <div class="bs-title">Backend Detail</div>
            <div id="bs-health-content"></div>
        </div>
    </div>
    <div id="bs-models" class="bottom-sheet" aria-hidden="true" role="dialog" aria-label="Model controls">
        <div class="bs-handle" data-bs-handle></div>
        <div class="bs-content">
            <div class="bs-title">LLM Model Control</div>
            <div id="bs-models-content"></div>
        </div>
    </div>
    <div id="bs-overflow" class="bottom-sheet" aria-hidden="true" role="dialog" aria-label="Operator tools">
        <div class="bs-handle" data-bs-handle></div>
        <div class="bs-content">
            <div class="bs-title">Operator Tools</div>
            <div id="bs-overflow-content"></div>
        </div>
    </div>

    <div id="model-modal" class="model-modal" aria-hidden="true">
        <section class="model-modal-panel" role="dialog" aria-modal="true" aria-labelledby="model-modal-title">
            <div class="model-modal-header">
                <h2 id="model-modal-title" class="model-modal-title">Model Metadata</h2>
                <button id="model-modal-close" class="btn unload focusable" type="button">Close</button>
            </div>
            <div id="model-modal-subtitle" class="endpoint-selected-backend">Select a model in the registry</div>
            <div id="model-modal-live" class="model-modal-live" aria-live="polite">Live registry: waiting for refresh...</div>
            <div id="model-modal-content"></div>
        </section>
    </div>
    <div id="sr-live-region" class="sr-only" aria-live="polite"></div>

    <script>
        let uptimeSeconds = __UPTIME_SECONDS__;
        let modelActionInFlight = false;
        let healthRequestToken = 0;
        let modelsRequestToken = 0;
        let latestHealthToken = 0;
        let latestModelsToken = 0;
        let healthAbortController = null;
        let modelsAbortController = null;
        let healthStaleAnnounced = false;
        let modelsStaleAnnounced = false;

        const healthState = {
            status: 'checking',
            healthy: 0,
            total: 0,
            stale: true,
            lastSuccessMs: 0,
        };

        const modelState = {
            loaded: 0,
            loading: 0,
            failed: 0,
            total: 0,
            stale: true,
            lastSuccessMs: 0,
        };
        const modelRegistry = new Map();
        const modelProfileSchemaCache = new Map();
        const modelPendingActions = new Map();
        let modelRegistryRefreshedAt = 0;
        let activeModelModalId = '';
        let selectedBackend = '';
        const TERMINAL_FEED_MODE = "__TERMINAL_FEED_MODE__";
        const TERMINAL_INSTANCE_ID = "__INSTANCE_ID__";
        const TERMINAL_MAX_LINES = 300;
        const TERMINAL_RECONNECT_MS = 2500;
        let terminalEventSource = null;
        let terminalReconnectTimer = null;
        let terminalExpanded = false;
        const terminalLevelFilter = new Set(['INFO', 'WARNING', 'ERROR', 'CRITICAL']);

        try {
            terminalExpanded = localStorage.getItem('synapse_terminal_expanded') === '1';
        } catch (e) {}

        /* --- Bottom Sheet Controller --- */
        let activeBottomSheet = null;
        let bsDragStartY = 0;
        let bsDragCurrentY = 0;
        let bsDragging = false;
        const isMobile = () => window.matchMedia('(max-width: 700px)').matches;

        function openBottomSheet(id) {
            closeAllBottomSheets();
            const sheet = document.getElementById(id);
            const backdrop = document.getElementById('bs-backdrop');
            if (!sheet || !backdrop) return;
            activeBottomSheet = id;
            backdrop.classList.add('active');
            backdrop.setAttribute('aria-hidden', 'false');
            sheet.classList.add('open');
            sheet.setAttribute('aria-hidden', 'false');
            document.body.style.overflow = 'hidden';
        }

        function closeAllBottomSheets() {
            const bsModels = document.getElementById('bs-models-content');
            if (bsModels && bsModels.dataset.sourcePanel) {
                const target = document.querySelector('.dashboard-grid > .panel:last-child');
                if (target) {
                    while (bsModels.firstChild) target.appendChild(bsModels.firstChild);
                }
                delete bsModels.dataset.sourcePanel;
            }
            const backdrop = document.getElementById('bs-backdrop');
            if (backdrop) {
                backdrop.classList.remove('active');
                backdrop.setAttribute('aria-hidden', 'true');
            }
            document.querySelectorAll('.bottom-sheet.open').forEach(s => {
                s.classList.remove('open');
                s.setAttribute('aria-hidden', 'true');
            });
            const modal = document.getElementById('model-modal');
            if (!modal || !modal.classList.contains('open')) {
                document.body.style.overflow = '';
            }
            activeBottomSheet = null;
        }

        /* --- Mobile Health Chips --- */
        function renderMobileHealthChips() {
            if (!isMobile()) return;
            const container = document.getElementById('mobile-health-chips');
            if (!container) return;
            const cards = document.querySelectorAll('.backend-card[data-backend]');
            container.innerHTML = '';
            cards.forEach(card => {
                const name = card.getAttribute('data-backend');
                const dotEl = card.querySelector('.status-dot');
                const statusCls = dotEl ? dotEl.className.replace('status-dot', '').trim() : 'checking';
                const chip = document.createElement('button');
                chip.className = 'mobile-health-chip';
                chip.setAttribute('data-chip-backend', name);
                chip.setAttribute('type', 'button');
                chip.setAttribute('aria-label', name + ' - ' + statusCls);
                chip.innerHTML = '<span class="chip-dot ' + statusCls + '"></span>' + escapeHtml(name);
                container.appendChild(chip);
            });
        }

        /* --- Mobile Model Bar --- */
        function updateMobileModelBar() {
            if (!isMobile()) return;
            const nameEl = document.getElementById('mobile-model-name');
            const statusEl = document.getElementById('mobile-model-status');
            if (!nameEl || !statusEl) return;
            let activeModel = null;
            for (const [, m] of modelRegistry) {
                const sv = m.status && m.status.value;
                if (sv === 'loaded' || sv === 'loading') { activeModel = m; break; }
            }
            if (activeModel) {
                nameEl.textContent = activeModel.id || 'Unknown';
                const sv = activeModel.status ? activeModel.status.value : 'unknown';
                const fl = Boolean(activeModel.status && activeModel.status.failed);
                statusEl.innerHTML = '<span class="status-pill ' + statusClass(sv, fl) + '">' + escapeHtml(fl ? 'failed' : sv) + '</span>';
            } else {
                nameEl.textContent = modelState.total > 0 ? modelState.total + ' models registered' : 'No models';
                statusEl.innerHTML = '<span class="status-pill unloaded">idle</span>';
            }
        }

        /* --- Mobile Filter Dropdown --- */
        function initMobileFilter() {
            const btn = document.getElementById('mobile-filter-btn');
            const dropdown = document.getElementById('mobile-filter-dropdown');
            if (!btn || !dropdown) return;
            btn.addEventListener('click', (e) => {
                e.stopPropagation();
                const isOpen = dropdown.classList.toggle('open');
                btn.setAttribute('aria-expanded', isOpen ? 'true' : 'false');
            });
            document.addEventListener('click', () => {
                dropdown.classList.remove('open');
                btn.setAttribute('aria-expanded', 'false');
            });
        }

        const BACKEND_ENDPOINTS = {
            'llama-embed': {
                title: 'llama-embed',
                groups: [
                    {
                        title: 'Gateway Route',
                        routes: [{method: 'POST', path: '/v1/embeddings'}],
                    },
                    {
                        title: 'Ops',
                        routes: [{method: 'GET', path: '/health'}],
                    },
                ],
            },
            'llama-router': {
                title: 'llama-router',
                groups: [
                    {
                        title: 'Gateway Routes',
                        routes: [
                            {method: 'POST', path: '/v1/chat/completions'},
                            {method: 'GET', path: '/models'},
                            {method: 'POST', path: '/models/load'},
                            {method: 'POST', path: '/models/unload'},
                        ],
                    },
                    {
                        title: 'Ops',
                        routes: [{method: 'GET', path: '/health'}],
                    },
                ],
            },
            'chatterbox-tts': {
                title: 'chatterbox-tts',
                groups: [
                    {
                        title: 'Gateway Routes',
                        routes: [
                            {method: 'POST', path: '/tts/synthesize'},
                            {method: 'POST', path: '/tts/stream'},
                            {method: 'POST', path: '/tts/interpolate'},
                            {method: 'GET', path: '/tts/languages'},
                        ],
                    },
                    {
                        title: 'Ops',
                        routes: [{method: 'GET', path: '/health'}],
                    },
                ],
            },
            'whisper-stt': {
                title: 'whisper-stt',
                groups: [
                    {
                        title: 'Gateway Routes',
                        routes: [
                            {method: 'POST', path: '/stt/transcribe'},
                            {method: 'POST', path: '/stt/detect-language'},
                            {method: 'POST', path: '/stt/stream'},
                        ],
                    },
                    {
                        title: 'Ops',
                        routes: [{method: 'GET', path: '/health'}],
                    },
                ],
            },
            'pyannote-speaker': {
                title: 'pyannote-speaker',
                groups: [
                    {
                        title: 'Gateway Routes',
                        routes: [
                            {method: 'POST', path: '/speakers/diarize'},
                            {method: 'POST', path: '/speakers/verify'},
                        ],
                    },
                    {
                        title: 'Ops',
                        routes: [{method: 'GET', path: '/health'}],
                    },
                ],
            },
            'deepfilter-audio': {
                title: 'deepfilter-audio',
                groups: [
                    {
                        title: 'Gateway Routes',
                        routes: [
                            {method: 'POST', path: '/audio/denoise'},
                            {method: 'POST', path: '/audio/convert'},
                        ],
                    },
                    {
                        title: 'Ops',
                        routes: [{method: 'GET', path: '/health'}],
                    },
                ],
            },
        };

        const MODEL_CARD_HINTS = [
            {
                pattern: /Nemotron-Nano-3-30B-A3B/i,
                links: [
                    {label: 'NVIDIA BF16 Card', url: 'https://huggingface.co/nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-Base-BF16'},
                    {label: 'GGUF Conversion', url: 'https://huggingface.co/ggml-org/Nemotron-Nano-3-30B-A3B-GGUF'},
                ],
            },
            {
                pattern: /gpt-oss-120b/i,
                links: [{label: 'GGUF Card', url: 'https://huggingface.co/unsloth/gpt-oss-120b-GGUF'}],
            },
            {
                pattern: /MiniMax-M2\\.5/i,
                links: [{label: 'GGUF Card', url: 'https://huggingface.co/unsloth/MiniMax-M2.5-GGUF'}],
            },
            {
                pattern: /Qwen3-8B-Q4_K_M/i,
                links: [{label: 'GGUF Card', url: 'https://huggingface.co/unsloth/Qwen3-8B-GGUF'}],
            },
            {
                pattern: /Qwen2\\.5-Coder-7B-Instruct-Q4_K_M/i,
                links: [{label: 'GGUF Card', url: 'https://huggingface.co/unsloth/Qwen2.5-Coder-7B-Instruct-GGUF'}],
            },
            {
                pattern: /glm-4\\.7-flash-claude-4\\.5-opus/i,
                links: [{label: 'GGUF Card', url: 'https://huggingface.co/TeichAI/GLM-4.7-Flash-Claude-Opus-4.5-High-Reasoning-Distill-GGUF'}],
            },
        ];

        function announce(message) {
            const region = document.getElementById('sr-live-region');
            if (!region) return;
            region.textContent = '';
            setTimeout(() => {
                region.textContent = message;
            }, 10);
        }


        function setTerminalConnection(state, detail = '') {
            const el = document.getElementById('terminal-conn');
            if (!el) return;
            el.className = `terminal-conn ${state}`;
            const suffix = detail ? ` (${detail})` : '';
            el.textContent = `${state}${suffix}`;
        }

        function appendTerminalLine(entry, opts = {}) {
            const host = document.getElementById('terminal-feed');
            if (!host) return;
            const shouldStick = host.scrollTop + host.clientHeight >= host.scrollHeight - 24;
            const line = document.createElement('div');
            const levelValue = String(entry.level || 'INFO').toUpperCase();
            line.className = 'term-line' + (opts.system ? ' system' : '');
            line.dataset.level = levelValue;

            if (!opts.system && !terminalLevelFilter.has(levelValue)) {
                line.style.display = 'none';
            }

            const level = document.createElement('span');
            level.className = 'term-level';
            const levelTone = levelValue.toLowerCase();
            if (levelTone === 'warning' || levelTone === 'error' || levelTone === 'critical') {
                level.classList.add(levelTone);
            }
            level.textContent = levelValue;

            const source = document.createElement('span');
            source.className = 'term-src';
            source.textContent = String(entry.source || 'gateway');

            const msg = document.createElement('span');
            msg.className = 'term-msg';
            msg.textContent = String(entry.message || '');

            line.appendChild(level);
            line.appendChild(source);
            line.appendChild(msg);
            host.appendChild(line);

            while (host.children.length > TERMINAL_MAX_LINES) {
                host.removeChild(host.firstElementChild);
            }
            if (shouldStick) {
                host.scrollTop = host.scrollHeight;
            }
        }

        function toggleTerminal() {
            terminalExpanded = !terminalExpanded;
            const body = document.getElementById('terminal-feed');
            const btn = document.getElementById('terminal-toggle');
            if (body) body.classList.toggle('expanded', terminalExpanded);
            if (btn) btn.textContent = terminalExpanded ? 'Collapse' : 'Expand';
            try { localStorage.setItem('synapse_terminal_expanded', terminalExpanded ? '1' : '0'); } catch (e) {}
        }

        function toggleTerminalLevel(level) {
            if (terminalLevelFilter.has(level)) {
                terminalLevelFilter.delete(level);
            } else {
                terminalLevelFilter.add(level);
            }
            document.querySelectorAll('.terminal-filter-btn[data-level]').forEach(btn => {
                btn.classList.toggle('active', terminalLevelFilter.has(btn.dataset.level));
            });
            const host = document.getElementById('terminal-feed');
            if (!host) return;
            host.querySelectorAll('.term-line[data-level]').forEach(line => {
                line.style.display = terminalLevelFilter.has(line.dataset.level) ? '' : 'none';
            });
        }

        function initTerminalControls() {
            const toggle = document.getElementById('terminal-toggle');
            if (toggle) toggle.addEventListener('click', toggleTerminal);
            document.querySelectorAll('.terminal-filter-btn[data-level]').forEach(btn => {
                btn.addEventListener('click', () => toggleTerminalLevel(btn.dataset.level));
            });
            if (terminalExpanded) {
                const body = document.getElementById('terminal-feed');
                if (body) body.classList.add('expanded');
                if (toggle) toggle.textContent = 'Collapse';
            }
        }

        function connectTerminalFeed() {
            if (TERMINAL_FEED_MODE !== 'live') {
                setTerminalConnection('stale', 'mock mode');
                appendTerminalLine(
                    {level: 'INFO', source: 'gateway.bootstrap', message: `terminal feed mode=${TERMINAL_FEED_MODE} instance=${TERMINAL_INSTANCE_ID}`},
                    {system: true},
                );
                return;
            }
            if (terminalEventSource) {
                terminalEventSource.close();
            }
            setTerminalConnection('connecting');
            terminalEventSource = new EventSource('/events/terminal');

            terminalEventSource.addEventListener('log', (event) => {
                try {
                    const payload = JSON.parse(event.data || '{}');
                    appendTerminalLine(payload);
                } catch (e) {
                    appendTerminalLine({level: 'WARNING', source: 'gateway.ui', message: 'failed to parse terminal event'}, {system: true});
                }
            });

            terminalEventSource.addEventListener('meta', (event) => {
                try {
                    const payload = JSON.parse(event.data || '{}');
                    const instance = payload && payload.instance ? String(payload.instance) : TERMINAL_INSTANCE_ID;
                    const bus = payload && payload.bus_mode ? String(payload.bus_mode) : 'local';
                    const detail = `${instance}/${bus}`;
                    setTerminalConnection('live', detail);
                } catch (e) {
                    setTerminalConnection('live', TERMINAL_INSTANCE_ID);
                }
            });

            terminalEventSource.onopen = () => {
                setTerminalConnection('live', TERMINAL_INSTANCE_ID);
            };

            terminalEventSource.onerror = () => {
                setTerminalConnection('stale', 'reconnecting');
                if (terminalEventSource) {
                    terminalEventSource.close();
                    terminalEventSource = null;
                }
                if (terminalReconnectTimer) {
                    clearTimeout(terminalReconnectTimer);
                }
                terminalReconnectTimer = setTimeout(() => {
                    connectTerminalFeed();
                }, TERMINAL_RECONNECT_MS);
            };
        }

        function updateEndpointTotal(total = null) {
            const el = document.getElementById('endpoint-total');
            if (!el) return;
            if (typeof total === 'number') {
                el.textContent = `${total} total`;
                return;
            }
            const count = document.querySelectorAll('#backend-endpoint-groups .endpoint-row').length;
            el.textContent = `${count} total`;
        }

        function formatTimestampFromUnix(seconds) {
            const n = Number(seconds);
            if (!Number.isFinite(n) || n <= 0) return '-';
            const dt = new Date(n * 1000);
            return `${dt.toLocaleString()} (${Math.floor(n)})`;
        }

        function parseRuntimeArgs(args) {
            if (!Array.isArray(args)) return [];
            const rows = [];
            for (let i = 0; i < args.length; i++) {
                const token = args[i];
                if (typeof token !== 'string') {
                    rows.push({key: `arg[${i}]`, value: String(token)});
                    continue;
                }
                if (token.startsWith('--')) {
                    let value = 'true';
                    const next = args[i + 1];
                    if (typeof next === 'string' && !next.startsWith('--')) {
                        value = next;
                        i += 1;
                    }
                    rows.push({key: token, value});
                    continue;
                }
                rows.push({key: `arg[${i}]`, value: token});
            }
            return rows;
        }

        const RUNTIME_ARG_HELP = {
            '--ctx-size': 'Maximum context window in tokens for a single request.',
            '--threads': 'CPU threads for token generation.',
            '--threads-batch': 'CPU threads for prompt/batch processing.',
            '--batch-size': 'Batch token count used during prompt evaluation.',
            '--ubatch-size': 'Micro-batch token count for compute scheduling.',
            '--parallel': 'Number of concurrent slots handled by the model server.',
            '--sleep-idle-seconds': 'Auto-idle timeout before model sleeps.',
            '--model': 'Path to the GGUF file loaded by llama.cpp.',
            '--port': 'Internal llama.cpp child server port.',
            '--host': 'Internal bind host for llama.cpp child server.',
            '--metrics': 'Enables llama.cpp metrics endpoint.',
            '--alias': 'Model alias exposed by router mode.',
        };

        function runtimeArgHelp(key) {
            const norm = String(key || '').toLowerCase();
            return RUNTIME_ARG_HELP[norm] || 'Runtime argument from llama.cpp preset.';
        }

        function renderRuntimeArgs(args) {
            const pairs = parseRuntimeArgs(args);
            if (pairs.length === 0) {
                return '<div class="endpoint-empty">No runtime args exposed.</div>';
            }
            return `<div class="model-arg-grid">${pairs.map((pair) => `
                <div class="model-arg-row">
                    <div class="model-arg-key">${escapeHtml(pair.key)}</div>
                    <div class="model-arg-value">
                        ${escapeHtml(pair.value)}
                        ${pair.key.startsWith('--') ? `<div class="model-arg-help">${escapeHtml(runtimeArgHelp(pair.key))}</div>` : ''}
                    </div>
                </div>`).join('')}</div>`;
        }

        function parseRuntimeArgMap(args) {
            const map = {};
            const pairs = parseRuntimeArgs(args);
            pairs.forEach((pair) => {
                if (typeof pair.key === 'string' && pair.key.startsWith('--')) {
                    map[pair.key] = String(pair.value ?? '');
                }
            });
            return map;
        }

        function inferModelCardLinks(modelId) {
            const id = String(modelId || '');
            for (const hint of MODEL_CARD_HINTS) {
                if (hint.pattern.test(id)) {
                    return hint.links;
                }
            }
            return [{
                label: 'Hugging Face Search',
                url: `https://huggingface.co/models?search=${encodeURIComponent(id)}`,
            }];
        }

        function renderSourceRows(rows) {
            if (!Array.isArray(rows) || rows.length === 0) {
                return '<div class="model-source-meta">No values available.</div>';
            }
            return rows.map((row) => `
                <div class="model-source-row">
                    <div class="model-source-key">${escapeHtml(row.key || '-')}</div>
                    <div class="model-source-value">${escapeHtml(row.value || '-')}</div>
                </div>
            `).join('');
        }

        function renderSourceTruthPanel(modelId, schema, profilePayload) {
            const model = modelRegistry.get(modelId) || {};
            const status = model.status || {};
            const runtimeMap = parseRuntimeArgMap(Array.isArray(status.args) ? status.args : []);
            const profileValues = (profilePayload && typeof profilePayload === 'object' && profilePayload.values && typeof profilePayload.values === 'object')
                ? profilePayload.values
                : {};
            const fields = Array.isArray(schema?.fields) ? schema.fields : [];

            const runtimeRows = [
                {key: 'status', value: String(status.value || 'unknown')},
                {key: '--ctx-size', value: String(runtimeMap['--ctx-size'] || '-')},
                {key: '--batch-size', value: String(runtimeMap['--batch-size'] || '-')},
                {key: '--ubatch-size', value: String(runtimeMap['--ubatch-size'] || '-')},
                {key: '--threads', value: String(runtimeMap['--threads'] || '-')},
                {key: '--threads-batch', value: String(runtimeMap['--threads-batch'] || '-')},
                {key: '--parallel', value: String(runtimeMap['--parallel'] || '-')},
            ];

            const profileRows = Object.entries(profileValues).map(([key, value]) => ({
                key,
                value: value === null || value === undefined ? '(unset)' : String(value),
            }));

            const boundRows = fields
                .filter((field) => field && (field.type === 'number' || field.type === 'integer'))
                .map((field) => ({
                    key: field.name || 'param',
                    value: `${field.min ?? '-'} .. ${field.max ?? '-'} (default ${field.default ?? '-'})`,
                }));

            const links = inferModelCardLinks(modelId).map((link) => (
                `<a class="model-source-link" href="${escapeHtml(link.url)}" target="_blank" rel="noopener noreferrer">${escapeHtml(link.label)}</a>`
            )).join(' · ');

            return `
                <div class="model-source-grid">
                    <div class="model-source-card">
                        <div class="model-source-title">Effective Runtime (Live)</div>
                        ${renderSourceRows(runtimeRows)}
                        <div class="model-source-meta">Source: <code>GET /models</code> → <code>status.args</code></div>
                    </div>
                    <div class="model-source-card">
                        <div class="model-source-title">Saved Model Profile</div>
                        ${renderSourceRows(profileRows)}
                        <div class="model-source-meta">Source: <code>GET /models/{id}/profile</code></div>
                    </div>
                    <div class="model-source-card">
                        <div class="model-source-title">Schema Min/Max Bounds</div>
                        ${renderSourceRows(boundRows)}
                        <div class="model-source-meta">Source: <code>GET /models/{id}/schema</code></div>
                    </div>
                    <div class="model-source-card">
                        <div class="model-source-title">Reference Model Cards</div>
                        <div class="model-source-meta">${links}</div>
                        <div class="model-source-meta">Source: Hugging Face model cards (reference only).</div>
                    </div>
                </div>
            `;
        }

        function encodeDomId(value) {
            return String(value).replace(/[^a-zA-Z0-9_-]/g, '_');
        }

        function modelProfileInputId(modelId, fieldName) {
            return `profile_${encodeDomId(modelId)}_${encodeDomId(fieldName)}`;
        }

        function setModelProfileStatus(message, isError = false) {
            const el = document.getElementById('model-profile-status');
            if (!el) return;
            el.className = 'model-profile-status' + (isError ? ' error' : '');
            el.textContent = message;
        }

        async function fetchModelProfileSchema(modelId) {
            const resp = await fetch(`/models/${encodeURIComponent(modelId)}/schema`, {cache: 'no-store'});
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(extractApiErrorMessage(data, resp.status));
            }
            return data;
        }

        async function fetchModelProfileValues(modelId) {
            const resp = await fetch(`/models/${encodeURIComponent(modelId)}/profile`, {cache: 'no-store'});
            const data = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(extractApiErrorMessage(data, resp.status));
            }
            return data;
        }

        function renderProfileInput(modelId, field, currentValue) {
            const inputId = modelProfileInputId(modelId, field.name);
            const type = field.type || 'string';
            const value = (currentValue === undefined || currentValue === null) ? '' : String(currentValue);
            if (type === 'enum') {
                const choices = Array.isArray(field.choices) ? field.choices : [];
                const options = ['<option value="">(unset)</option>'].concat(
                    choices.map((choice) => {
                        const selected = value === String(choice) ? ' selected' : '';
                        return `<option value="${escapeHtml(choice)}"${selected}>${escapeHtml(choice)}</option>`;
                    }),
                ).join('');
                return `<select id="${escapeHtml(inputId)}" class="model-profile-select">${options}</select>`;
            }
            if (type === 'string') {
                return `<textarea id="${escapeHtml(inputId)}" class="model-profile-textarea" placeholder="(unset)">${escapeHtml(value)}</textarea>`;
            }
            const htmlType = 'number';
            const minAttr = field.min !== undefined ? ` min="${escapeHtml(String(field.min))}"` : '';
            const maxAttr = field.max !== undefined ? ` max="${escapeHtml(String(field.max))}"` : '';
            const stepAttr = field.step !== undefined ? ` step="${escapeHtml(String(field.step))}"` : '';
            const sliderId = `${inputId}__slider`;
            const valueBadgeId = `${inputId}__value`;
            const sliderValue = value !== ''
                ? value
                : (field.default !== undefined && field.default !== null && field.default !== ''
                    ? String(field.default)
                    : (field.min !== undefined ? String(field.min) : '0'));
            const hasRange = field.min !== undefined && field.max !== undefined;
            if (!hasRange) {
                return `<input id="${escapeHtml(inputId)}" class="model-profile-input" type="${htmlType}"${minAttr}${maxAttr}${stepAttr} value="${escapeHtml(value)}" placeholder="(unset)">`;
            }
            return `
                <div class="model-profile-range-wrap">
                    <input id="${escapeHtml(inputId)}" class="model-profile-input" type="${htmlType}"${minAttr}${maxAttr}${stepAttr} value="${escapeHtml(value)}" placeholder="(unset)">
                    <input id="${escapeHtml(sliderId)}" class="model-profile-slider" type="range"${minAttr}${maxAttr}${stepAttr} value="${escapeHtml(sliderValue)}">
                    <div class="model-profile-range-meta">
                        <span>min ${escapeHtml(String(field.min))}</span>
                        <span id="${escapeHtml(valueBadgeId)}" class="model-profile-range-value">${escapeHtml(value || '(unset)')}</span>
                        <span>max ${escapeHtml(String(field.max))}</span>
                    </div>
                </div>
            `;
        }

        function bindProfileRangeInputs(modelId, schema) {
            const fields = Array.isArray(schema?.fields) ? schema.fields : [];
            fields.forEach((field) => {
                if (!field || (field.type !== 'number' && field.type !== 'integer')) return;
                if (field.min === undefined || field.max === undefined) return;
                const inputId = modelProfileInputId(modelId, field.name);
                const sliderId = `${inputId}__slider`;
                const valueBadgeId = `${inputId}__value`;
                const inputEl = document.getElementById(inputId);
                const sliderEl = document.getElementById(sliderId);
                const valueBadgeEl = document.getElementById(valueBadgeId);
                if (!inputEl || !sliderEl) return;

                const min = Number(field.min);
                const max = Number(field.max);

                const setValueBadge = (raw) => {
                    if (!valueBadgeEl) return;
                    valueBadgeEl.textContent = raw === '' ? '(unset)' : String(raw);
                };

                const clampValue = (raw) => {
                    const n = Number(raw);
                    if (!Number.isFinite(n)) return null;
                    let clamped = Math.max(min, Math.min(max, n));
                    if (field.type === 'integer') {
                        clamped = Math.round(clamped);
                    }
                    return clamped;
                };

                sliderEl.addEventListener('input', () => {
                    const n = clampValue(sliderEl.value);
                    if (n === null) return;
                    inputEl.value = String(n);
                    setValueBadge(String(n));
                });

                const syncFromInput = () => {
                    const raw = (inputEl.value || '').trim();
                    if (raw === '') {
                        setValueBadge('');
                        return;
                    }
                    const n = clampValue(raw);
                    if (n === null) return;
                    inputEl.value = String(n);
                    sliderEl.value = String(n);
                    setValueBadge(String(n));
                };

                inputEl.addEventListener('input', syncFromInput);
                inputEl.addEventListener('change', syncFromInput);

                syncFromInput();
            });
        }

        function renderProfileEditor(modelId, schema, values) {
            const fields = Array.isArray(schema?.fields) ? schema.fields : [];
            const notes = Array.isArray(schema?.notes) ? schema.notes : [];
            const rows = fields.map((field) => {
                const currentValue = values ? values[field.name] : undefined;
                const desc = field.description || '';
                const defaultText = field.default !== undefined && field.default !== ''
                    ? ` Default: ${field.default}.`
                    : '';
                return `
                    <div class="model-profile-row">
                        <div class="model-profile-topline">
                            <div class="model-profile-name">${escapeHtml(field.label || field.name || 'param')}</div>
                            <div class="model-profile-scope">${escapeHtml(field.applies_at || 'generation')}</div>
                        </div>
                        ${renderProfileInput(modelId, field, currentValue)}
                        <div class="model-profile-help">${escapeHtml(desc + defaultText)}</div>
                    </div>
                `;
            }).join('');
            const notesHtml = notes.length > 0
                ? `<div class="model-profile-notes">${notes.map((note) => `<div class="model-profile-note">${escapeHtml(String(note))}</div>`).join('')}</div>`
                : '';
            return `
                <div class="model-profile-grid">${rows}</div>
                <div class="model-profile-actions">
                    <button class="btn focusable" type="button" data-model-profile-save="${escapeHtml(modelId)}">Save Profile</button>
                    <button class="btn load focusable" type="button" data-model-profile-apply="${escapeHtml(modelId)}">Save + Load</button>
                    <button class="btn unload focusable" type="button" data-model-profile-reset="${escapeHtml(modelId)}">Reset Profile</button>
                    <span id="model-profile-status" class="model-profile-status">Profile loaded.</span>
                </div>
                ${notesHtml}
            `;
        }

        function collectModelProfileValues(modelId, schema) {
            const fields = Array.isArray(schema?.fields) ? schema.fields : [];
            const values = {};
            for (const field of fields) {
                const inputId = modelProfileInputId(modelId, field.name);
                const el = document.getElementById(inputId);
                if (!el) continue;
                const raw = (el.value || '').trim();
                if (raw === '') {
                    values[field.name] = null;
                    continue;
                }
                if (field.type === 'number') {
                    const n = Number(raw);
                    if (!Number.isFinite(n)) {
                        throw new Error(`Invalid value for ${field.label || field.name}`);
                    }
                    if (field.min !== undefined && n < Number(field.min)) {
                        throw new Error(`${field.label || field.name} must be >= ${field.min}`);
                    }
                    if (field.max !== undefined && n > Number(field.max)) {
                        throw new Error(`${field.label || field.name} must be <= ${field.max}`);
                    }
                    values[field.name] = n;
                    continue;
                }
                if (field.type === 'integer') {
                    const n = Number(raw);
                    if (!Number.isInteger(n)) {
                        throw new Error(`${field.label || field.name} must be an integer`);
                    }
                    if (field.min !== undefined && n < Number(field.min)) {
                        throw new Error(`${field.label || field.name} must be >= ${field.min}`);
                    }
                    if (field.max !== undefined && n > Number(field.max)) {
                        throw new Error(`${field.label || field.name} must be <= ${field.max}`);
                    }
                    values[field.name] = n;
                    continue;
                }
                if (field.type === 'enum') {
                    values[field.name] = raw.toLowerCase();
                    continue;
                }
                values[field.name] = raw;
            }
            return values;
        }

        async function loadModelProfileEditor(modelId) {
            const host = document.getElementById('model-profile-shell');
            const sourceHost = document.getElementById('model-source-truth-shell');
            if (!host) return;
            host.innerHTML = '<div class="endpoint-empty">Loading profile schema...</div>';
            if (sourceHost) {
                sourceHost.innerHTML = '<div class="endpoint-empty">Loading source-of-truth data...</div>';
            }
            try {
                const [schema, profile] = await Promise.all([
                    fetchModelProfileSchema(modelId),
                    fetchModelProfileValues(modelId),
                ]);
                modelProfileSchemaCache.set(modelId, schema);
                host.innerHTML = renderProfileEditor(modelId, schema, profile.values || {});
                bindProfileRangeInputs(modelId, schema);
                if (sourceHost) {
                    sourceHost.innerHTML = renderSourceTruthPanel(modelId, schema, profile);
                }
                setModelProfileStatus('Profile loaded.');
            } catch (e) {
                host.innerHTML = `<div class="endpoint-empty" style="color:#ff8fa6">Profile load failed: ${escapeHtml(e.message || String(e))}</div>`;
                if (sourceHost) {
                    sourceHost.innerHTML = `<div class="endpoint-empty" style="color:#ff8fa6">Source-of-truth load failed: ${escapeHtml(e.message || String(e))}</div>`;
                }
            }
        }

        async function resetModelProfile(modelId) {
            const resp = await fetch(`/models/${encodeURIComponent(modelId)}/profile`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({values: {}, replace: true}),
            });
            const payload = await resp.json().catch(() => ({}));
            if (!resp.ok) {
                throw new Error(extractApiErrorMessage(payload, resp.status));
            }
            return payload;
        }

        async function saveModelProfile(modelId, loadModel = false) {
            const schema = modelProfileSchemaCache.get(modelId);
            if (!schema) {
                throw new Error('Profile schema not loaded yet');
            }
            const values = collectModelProfileValues(modelId, schema);
            const saveResp = await fetch(`/models/${encodeURIComponent(modelId)}/profile`, {
                method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({values}),
            });
            const savePayload = await saveResp.json().catch(() => ({}));
            if (!saveResp.ok) {
                throw new Error(extractApiErrorMessage(savePayload, saveResp.status));
            }
            if (loadModel) {
                const applyResp = await fetch(`/models/${encodeURIComponent(modelId)}/profile/apply`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify({load_model: true}),
                });
                const applyPayload = await applyResp.json().catch(() => ({}));
                if (!applyResp.ok) {
                    throw new Error(extractApiErrorMessage(applyPayload, applyResp.status));
                }
                if (applyPayload.load && applyPayload.load.success === false) {
                    throw new Error(`Model load failed (status ${applyPayload.load.status_code || 'unknown'})`);
                }
            }
            return savePayload;
        }

        function setModalOpenState(isOpen) {
            const modal = document.getElementById('model-modal');
            if (!modal) return;
            modal.classList.toggle('open', isOpen);
            modal.setAttribute('aria-hidden', isOpen ? 'false' : 'true');
            document.body.style.overflow = isOpen ? 'hidden' : '';
        }

        function updateModelModalLiveInfo(stale = false) {
            const liveEl = document.getElementById('model-modal-live');
            if (!liveEl || !activeModelModalId) return;
            const refreshStamp = modelRegistryRefreshedAt ? new Date(modelRegistryRefreshedAt).toLocaleTimeString() : '-';
            if (stale) {
                liveEl.textContent = `Live registry: refresh failed · showing snapshot from ${refreshStamp}`;
                liveEl.classList.add('stale');
                return;
            }
            liveEl.textContent = `Live registry: updated ${refreshStamp} · auto-refresh 10s`;
            liveEl.classList.remove('stale');
        }

        function closeModelModal() {
            activeModelModalId = '';
            setModalOpenState(false);
            const liveEl = document.getElementById('model-modal-live');
            if (liveEl) {
                liveEl.textContent = 'Live registry: waiting for refresh...';
                liveEl.classList.remove('stale');
            }
        }

        function renderModelModal(modelId, shouldAnnounce = false) {
            const model = modelRegistry.get(modelId);
            if (!model) {
                setModelActionStatus(`Model metadata unavailable for ${modelId}`, true);
                return;
            }
            activeModelModalId = modelId;
            const modalTitle = document.getElementById('model-modal-title');
            const subtitle = document.getElementById('model-modal-subtitle');
            const content = document.getElementById('model-modal-content');
            if (!modalTitle || !subtitle || !content) return;

            const status = model && model.status ? model.status : {};
            const statusValue = status && status.value ? status.value : 'unknown';
            const args = Array.isArray(status.args) ? status.args : [];
            const refreshStamp = modelRegistryRefreshedAt ? new Date(modelRegistryRefreshedAt).toLocaleTimeString() : '-';

            modalTitle.textContent = `Model Metadata :: ${modelId}`;
            subtitle.textContent = `${modelId} :: llama.cpp runtime metadata`;
            content.innerHTML = `
                <div class="model-meta-grid">
                    <div class="model-meta-item">
                        <div class="model-meta-label">Model ID</div>
                        <div class="model-meta-value">${escapeHtml(model.id || '-')}</div>
                    </div>
                    <div class="model-meta-item">
                        <div class="model-meta-label">Object</div>
                        <div class="model-meta-value">${escapeHtml(model.object || '-')}</div>
                    </div>
                    <div class="model-meta-item">
                        <div class="model-meta-label">Owner</div>
                        <div class="model-meta-value">${escapeHtml(model.owned_by || '-')}</div>
                    </div>
                    <div class="model-meta-item">
                        <div class="model-meta-label">Created</div>
                        <div class="model-meta-value">${escapeHtml(formatTimestampFromUnix(model.created))}</div>
                    </div>
                    <div class="model-meta-item">
                        <div class="model-meta-label">Status</div>
                        <div class="model-meta-value">${escapeHtml(statusValue)}</div>
                    </div>
                    <div class="model-meta-item">
                        <div class="model-meta-label">Failed</div>
                        <div class="model-meta-value">${escapeHtml(String(Boolean(status.failed)))}</div>
                    </div>
                    <div class="model-meta-item">
                        <div class="model-meta-label">Args Count</div>
                        <div class="model-meta-value">${escapeHtml(String(args.length))}</div>
                    </div>
                    <div class="model-meta-item">
                        <div class="model-meta-label">Registry Refresh</div>
                        <div class="model-meta-value">${escapeHtml(refreshStamp)}</div>
                    </div>
                    <div class="model-meta-item">
                        <div class="model-meta-label">Source</div>
                        <div class="model-meta-value">GET /models</div>
                    </div>
                </div>
                <div class="panel-title">Runtime Args</div>
                ${renderRuntimeArgs(args)}
                <div class="panel-title">Source Of Truth</div>
                <div id="model-source-truth-shell" class="model-source-shell">
                    <div class="endpoint-empty">Loading source-of-truth data...</div>
                </div>
                <div class="panel-title">Model Profile (Generation + Runtime)</div>
                <div id="model-profile-shell" class="model-profile-shell">
                    <div class="endpoint-empty">Loading profile schema...</div>
                </div>
                <div class="panel-title">Raw Metadata Payload</div>
                <div class="model-json-wrap">
                    <pre class="model-json">${escapeHtml(JSON.stringify(model, null, 2))}</pre>
                </div>
            `;
            setModalOpenState(true);
            updateModelModalLiveInfo(false);
            loadModelProfileEditor(modelId);
            if (shouldAnnounce) {
                announce(`${modelId} metadata opened.`);
            }
        }

        function indexModelRegistry(models) {
            modelRegistry.clear();
            (Array.isArray(models) ? models : []).forEach((item) => {
                if (!item || !item.id) return;
                modelRegistry.set(String(item.id), item);
            });
            modelRegistryRefreshedAt = Date.now();
            // Keep modal editor state stable during background polling.
            // Re-rendering here replaces modal DOM and wipes unsaved profile edits.
            if (activeModelModalId && modelRegistry.has(activeModelModalId)) {
                updateModelModalLiveInfo(false);
            }
        }

        function endpointMethodClass(method) {
            const m = String(method || '').toLowerCase();
            if (m === 'get') return 'get';
            if (m === 'post') return 'post';
            if (m === 'delete') return 'delete';
            return 'post';
        }

        function renderBackendEndpoints(backendName) {
            const host = document.getElementById('backend-endpoint-groups');
            const label = document.getElementById('endpoint-selected-backend');
            if (!host || !label) return;

            const spec = BACKEND_ENDPOINTS[backendName];
            if (!spec) {
                label.textContent = `${backendName || 'unknown'} :: no route map`;
                host.innerHTML = `
                    <div class="endpoint-empty">No API mapping is registered for this backend.</div>
                    <div class="endpoint-group">
                        <div class="endpoint-group-title">Ops</div>
                        <div class="endpoint-row">
                            <span class="ep-method get">GET</span>
                            <span class="ep-path">/health</span>
                        </div>
                    </div>`;
                updateEndpointTotal(1);
                return;
            }

            label.textContent = `${spec.title} :: live route map`;
            let total = 0;
            host.innerHTML = spec.groups.map((group) => {
                const routes = Array.isArray(group.routes) ? group.routes : [];
                total += routes.length;
                const rows = routes.map((route) => `
                    <div class="endpoint-row">
                        <span class="ep-method ${endpointMethodClass(route.method)}">${escapeHtml(route.method)}</span>
                        <span class="ep-path">${escapeHtml(route.path)}</span>
                    </div>`).join('');
                return `
                    <div class="endpoint-group">
                        <div class="endpoint-group-title">${escapeHtml(group.title)}</div>
                        ${rows}
                    </div>`;
            }).join('');
            updateEndpointTotal(total);
        }

        function selectBackend(backendName, shouldAnnounce = false) {
            if (!backendName) return;
            selectedBackend = backendName;
            document.querySelectorAll('.backend-card[data-backend]').forEach((card) => {
                const isActive = card.getAttribute('data-backend') === backendName;
                card.classList.toggle('active', isActive);
                card.setAttribute('aria-pressed', isActive ? 'true' : 'false');
            });
            renderBackendEndpoints(backendName);
            if (shouldAnnounce) {
                announce(`${backendName} endpoints loaded.`);
            }
        }

        function initializeBackendSelector() {
            const cards = Array.from(document.querySelectorAll('.backend-card[data-backend]'));
            if (cards.length === 0) {
                renderBackendEndpoints('');
                return;
            }
            const preferred = cards.find((card) => card.getAttribute('data-backend') === 'llama-router') || cards[0];
            selectBackend(preferred.getAttribute('data-backend'));
        }

        function escapeHtml(value) {
            return String(value)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#039;');
        }

        function statusClass(value, failed) {
            if (failed) return 'failed';
            if (value === 'loaded') return 'loaded';
            if (value === 'loading') return 'loading';
            if (value === 'unloading') return 'unloading';
            if (value === 'unloaded') return 'unloaded';
            return 'unknown';
        }

        function extractApiErrorMessage(payload, httpStatus) {
            if (!payload) return `HTTP ${httpStatus}`;
            if (typeof payload.detail === 'string') return payload.detail;
            if (payload.error && typeof payload.error === 'string') return payload.error;
            if (payload.ERROR && typeof payload.ERROR.message === 'string') return payload.ERROR.message;
            if (payload.ERROR && typeof payload.ERROR.MESSAGE === 'string') return payload.ERROR.MESSAGE;
            const fallback = JSON.stringify(payload);
            return fallback && fallback !== '{}' ? fallback : `HTTP ${httpStatus}`;
        }

        function getRegistryModels() {
            return Array.from(modelRegistry.values());
        }


        function setModelActionStatus(message, isError = false) {
            const el = document.getElementById('model-action-status');
            if (!el) return;
            el.className = 'model-action-status' + (isError ? ' error' : '');
            el.textContent = message;
        }

        function setModelButtonsDisabled(disabled) {
            const buttons = document.querySelectorAll('button[data-model-action]');
            buttons.forEach((button) => {
                button.disabled = Boolean(disabled);
            });
        }

        function applyHealthStaleState(message) {
            const dot = document.getElementById('overall-dot');
            if (dot) dot.className = 'status-bar-dot stale';

            const cards = document.querySelectorAll('[data-backend]');
            cards.forEach((card) => {
                const sd = card.querySelector('.status-dot');
                const st = card.querySelector('.backend-status');
                if (sd) sd.className = 'status-dot stale';
                if (st) {
                    const base = st.dataset.lastKnownStatus || st.textContent || 'Unknown';
                    st.textContent = `${base} [STALE]`;
                }
            });

            const info = document.getElementById('refresh-info');
            if (info) info.textContent = `${message} · showing last known state`;

            healthState.stale = true;
        }


        function renderModelsTable(models) {
            const body = document.getElementById('models-table-body');
            if (!body) return;
            if (!Array.isArray(models) || models.length === 0) {
                body.innerHTML = '<tr><td colspan="3" style="color:#94a3b8">No models registered in llama-router.</td></tr>';
                return;
            }
            body.innerHTML = models.map((m) => {
                const id = m && m.id ? String(m.id) : 'unknown';
                const status = m && m.status ? m.status : {};
                const statusValue = status.value || 'unknown';
                const failed = Boolean(status.failed);
                const pendingAction = modelPendingActions.get(id) || '';
                let effectiveStatus = statusValue;
                let effectiveFailed = failed;
                if (pendingAction === 'load') {
                    effectiveStatus = 'loading';
                    effectiveFailed = false;
                } else if (pendingAction === 'unload') {
                    effectiveStatus = 'unloading';
                    effectiveFailed = false;
                }
                const canLoad = !modelActionInFlight && !pendingAction && effectiveStatus !== 'loaded' && effectiveStatus !== 'loading';
                const canUnload = !modelActionInFlight && !pendingAction && effectiveStatus === 'loaded';
                return `<tr>
                    <td><button class="model-link focusable" data-model-info="${escapeHtml(id)}" type="button">${escapeHtml(id)}</button></td>
                    <td><span class="status-pill ${statusClass(effectiveStatus, effectiveFailed)}">${escapeHtml(effectiveFailed ? 'failed' : effectiveStatus)}</span></td>
                    <td>
                        <div class="model-actions">
                            <button class="btn load" data-model-action="load" data-model-id="${escapeHtml(id)}" ${canLoad ? '' : 'disabled'}>Load</button>
                            <button class="btn unload" data-model-action="unload" data-model-id="${escapeHtml(id)}" ${canUnload ? '' : 'disabled'}>Unload</button>
                        </div>
                    </td>
                </tr>`;
            }).join('');
        }

        function wait(ms) {
            return new Promise((resolve) => setTimeout(resolve, ms));
        }

        async function waitForModelStatus(modelId, targetStatus, actionLabel, timeoutMs = 90000, intervalMs = 1500) {
            const deadline = Date.now() + timeoutMs;
            while (Date.now() < deadline) {
                await refreshModels(true);
                const model = modelRegistry.get(modelId);
                const status = model && model.status && model.status.value ? model.status.value : 'unknown';
                const failed = Boolean(model && model.status && model.status.failed);
                if (failed) {
                    throw new Error(`validation failed (${status})`);
                }
                if (status === targetStatus) {
                    return true;
                }
                setModelActionStatus(`${actionLabel} ${modelId}... validating (${status})`);
                await wait(intervalMs);
            }
            return false;
        }

        async function refreshModels(force = false) {
            const info = document.getElementById('models-refresh-info');
            if (modelActionInFlight && !force) return;
            const requestToken = ++modelsRequestToken;
            if (modelsAbortController) {
                modelsAbortController.abort();
            }
            modelsAbortController = new AbortController();

            try {
                const resp = await fetch('/models', {
                    signal: modelsAbortController.signal,
                    cache: 'no-store',
                });
                const data = await resp.json();
                if (!resp.ok) {
                    const detail = data && data.detail ? data.detail : `HTTP ${resp.status}`;
                    throw new Error(detail);
                }

                if (requestToken < latestModelsToken) return;
                latestModelsToken = requestToken;

                const models = Array.isArray(data.data) ? data.data : [];
                indexModelRegistry(models);
                renderModelsTable(models);
                updateMobileModelBar();
                const now = new Date();
                if (info) {
                    info.textContent = 'Last checked: ' + now.toLocaleTimeString() + ' · refreshes every 10s';
                }

                let loaded = 0;
                let loading = 0;
                let failed = 0;
                models.forEach((item) => {
                    const status = item && item.status ? item.status : {};
                    if (status.failed) {
                        failed += 1;
                    } else if (status.value === 'loaded') {
                        loaded += 1;
                    } else if (status.value === 'loading') {
                        loading += 1;
                    }
                });

                modelState.loaded = loaded;
                modelState.loading = loading;
                modelState.failed = failed;
                modelState.total = models.length;
                modelState.stale = false;
                modelState.lastSuccessMs = Date.now();
                models.forEach((item) => {
                    const id = item && item.id ? String(item.id) : '';
                    if (!id) return;
                    const pending = modelPendingActions.get(id);
                    if (!pending) return;
                    const value = item && item.status && item.status.value ? item.status.value : '';
                    if ((pending === 'load' && value === 'loaded') || (pending === 'unload' && value === 'unloaded')) {
                        modelPendingActions.delete(id);
                    }
                    if (item && item.status && item.status.failed) {
                        modelPendingActions.delete(id);
                    }
                });
                modelsStaleAnnounced = false;

            } catch (e) {
                if (e && e.name === 'AbortError') return;
                if (requestToken < latestModelsToken) return;
                latestModelsToken = requestToken;

                if (info) {
                    info.textContent = 'Model refresh failed · retrying in 10s';
                }
                setModelActionStatus('Model API unavailable: ' + e.message, true);
                const body = document.getElementById('models-table-body');
                if (body) {
                    body.innerHTML = '<tr><td colspan="3" style="color:#fca5a5">Failed to load /models.</td></tr>';
                }


                modelState.stale = true;

                updateModelModalLiveInfo(true);
                if (!modelsStaleAnnounced) {
                    announce('Model telemetry is stale.');
                    modelsStaleAnnounced = true;
                }
            }
        }

        async function runModelAction(action, modelId) {
            if (modelActionInFlight) {
                setModelActionStatus('Another model action is still running...');
                return;
            }
            const actionUpper = action.toUpperCase();
            const targetStatus = action === 'unload' ? 'unloaded' : 'loaded';
            const requestBody = {model: modelId};
            modelActionInFlight = true;
            setModelButtonsDisabled(true);
            modelPendingActions.set(modelId, action);
            renderModelsTable(getRegistryModels());
            setModelActionStatus(`${actionUpper} ${modelId}... in progress`);
            try {
                const resp = await fetch(`/models/${action}`, {
                    method: 'POST',
                    headers: {'Content-Type': 'application/json'},
                    body: JSON.stringify(requestBody),
                });
                const payload = await resp.json().catch(() => ({}));
                if (!resp.ok) {
                    const detail = extractApiErrorMessage(payload, resp.status);
                    const detailLc = String(detail).toLowerCase();
                    if (action === 'unload' && detailLc.includes('model is not loaded')) {
                        modelPendingActions.delete(modelId);
                        setModelActionStatus(`${actionUpper} ${modelId}: already unloaded`);
                        announce(`${modelId} was already unloaded.`);
                        await refreshModels(true);
                        return;
                    }
                    if (action === 'load' && detailLc.includes('already loaded')) {
                        modelPendingActions.delete(modelId);
                        setModelActionStatus(`${actionUpper} ${modelId}: already loaded`);
                        announce(`${modelId} was already loaded.`);
                        await refreshModels(true);
                        return;
                    }
                    throw new Error(detail || `HTTP ${resp.status}`);
                }
                setModelActionStatus(`${actionUpper} ${modelId}: request accepted, validating...`);
                const ok = await waitForModelStatus(modelId, targetStatus, actionUpper);
                if (!ok) {
                    throw new Error(`timed out waiting for ${targetStatus}`);
                }
                setModelActionStatus(`${actionUpper} ${modelId}: ${targetStatus}`);
                announce(`${actionUpper} ${modelId} confirmed ${targetStatus}.`);
            } catch (e) {
                setModelActionStatus(`${actionUpper} ${modelId}: ${e.message}`, true);
                announce(`${actionUpper} ${modelId} failed.`);
                modelPendingActions.delete(modelId);
            } finally {
                await refreshModels(true);
                modelActionInFlight = false;
                modelPendingActions.delete(modelId);
                renderModelsTable(getRegistryModels());
                setModelButtonsDisabled(false);

            }
        }

        function updateUptime() {
            uptimeSeconds++;
            const d = Math.floor(uptimeSeconds / 86400);
            const h = Math.floor((uptimeSeconds % 86400) / 3600);
            const m = Math.floor((uptimeSeconds % 3600) / 60);
            const s = uptimeSeconds % 60;
            const parts = [];
            if (d) parts.push(d + 'd');
            if (h) parts.push(h + 'h');
            if (m) parts.push(m + 'm');
            parts.push(s + 's');
            document.getElementById('uptime').textContent = parts.join(' ');
        }

        async function refreshHealth() {
            const requestToken = ++healthRequestToken;
            if (healthAbortController) {
                healthAbortController.abort();
            }
            healthAbortController = new AbortController();

            try {
                const resp = await fetch('/health', {
                    signal: healthAbortController.signal,
                    cache: 'no-store',
                });
                const data = await resp.json();

                if (requestToken < latestHealthToken) return;
                latestHealthToken = requestToken;

                const dot = document.getElementById('overall-dot');
                dot.className = 'status-bar-dot ' + (data.status === 'healthy' ? 'healthy' : 'degraded');
                const backends = data.backends || {};
                const backendEntries = Object.entries(backends);
                let healthyCount = 0;

                if (data.status !== 'healthy' && !healthStaleAnnounced) {
                    announce('Gateway health degraded.');
                    healthStaleAnnounced = true;
                }

                for (const [name, info] of Object.entries(backends)) {
                    const card = document.querySelector('[data-backend="' + name + '"]');
                    if (!card) continue;
                    const sd = card.querySelector('.status-dot');
                    const st = card.querySelector('.backend-status');
                    const status = info.status || 'unreachable';
                    if (status === 'healthy') healthyCount += 1;
                    sd.className = 'status-dot ' + status;
                    let label = status.charAt(0).toUpperCase() + status.slice(1);
                    if (info.code) label += ' (' + info.code + ')';
                    if (info.error) label += ' \u2014 ' + info.error.substring(0, 60);
                    st.textContent = label;
                    st.dataset.lastKnownStatus = label;
                    const mc = document.querySelector('[data-chip-backend="' + name + '"] .chip-dot');
                    if (mc) mc.className = 'chip-dot ' + status;
                }
                const now = new Date();
                document.getElementById('refresh-info').textContent =
                    'Last checked: ' + now.toLocaleTimeString() + ' \u00b7 refreshes every 30s';

                healthState.status = data.status === 'healthy' ? 'healthy' : 'degraded';
                healthState.healthy = healthyCount;
                healthState.total = backendEntries.length;
                healthState.stale = false;
                healthState.lastSuccessMs = Date.now();
                if (data.status === 'healthy') {
                    healthStaleAnnounced = false;
                }
                renderMobileHealthChips();

            } catch (e) {
                if (e && e.name === 'AbortError') return;
                if (requestToken < latestHealthToken) return;
                latestHealthToken = requestToken;
                applyHealthStaleState('Health refresh failed');
                if (!healthStaleAnnounced) {
                    announce('Health telemetry is stale.');
                    healthStaleAnnounced = true;
                }
            }
        }

        document.addEventListener('click', (event) => {
            /* --- Mobile: overflow button --- */
            if (event.target.closest('#mobile-overflow-btn')) {
                const c = document.getElementById('bs-overflow-content');
                if (c) {
                    const links = document.querySelector('.quick-links.inline');
                    c.innerHTML = links ? links.outerHTML : '';
                }
                openBottomSheet('bs-overflow');
                return;
            }

            /* --- Mobile: health chip tap --- */
            const healthChip = event.target.closest('.mobile-health-chip[data-chip-backend]');
            if (healthChip && isMobile()) {
                const backendName = healthChip.getAttribute('data-chip-backend');
                const card = document.querySelector('.backend-card[data-backend="' + backendName + '"]');
                const bsC = document.getElementById('bs-health-content');
                if (card && bsC) {
                    const dot = card.querySelector('.status-dot');
                    const dotCls = dot ? dot.className.replace('status-dot', '').trim() : 'checking';
                    const statusText = (card.querySelector('.backend-status') || {}).textContent || '';
                    const healthUrl = (card.querySelector('.backend-health-url') || {}).textContent || '';
                    const ep = BACKEND_ENDPOINTS[backendName];
                    let epHtml = '';
                    if (ep) {
                        epHtml = ep.groups.map(g =>
                            '<div class="endpoint-group"><div class="endpoint-group-title">' + escapeHtml(g.title) + '</div>' +
                            g.routes.map(r => '<div class="endpoint-row"><span class="ep-method">' + escapeHtml(r.method) + '</span><span class="ep-path">' + escapeHtml(r.path) + '</span></div>').join('') +
                            '</div>'
                        ).join('');
                    }
                    bsC.innerHTML =
                        '<div class="backend-card" style="clip-path:none;cursor:default;margin-bottom:0.75rem">' +
                        '<div class="backend-name"><span class="status-dot ' + dotCls + '"></span>' + escapeHtml(backendName) + '</div>' +
                        '<div class="backend-status">' + escapeHtml(statusText) + '</div>' +
                        '<div class="backend-health-url">' + escapeHtml(healthUrl) + '</div></div>' +
                        epHtml;
                }
                openBottomSheet('bs-health');
                return;
            }

            /* --- Mobile: model bar tap --- */
            if (event.target.closest('#mobile-model-bar') && isMobile()) {
                const bsC = document.getElementById('bs-models-content');
                const src = document.querySelector('.dashboard-grid > .panel:last-child');
                if (bsC && src) {
                    while (src.firstChild) bsC.appendChild(src.firstChild);
                    bsC.dataset.sourcePanel = '1';
                }
                openBottomSheet('bs-models');
                return;
            }

            const modalCloseBtn = event.target.closest('#model-modal-close');
            if (modalCloseBtn) {
                closeModelModal();
                return;
            }

            const modalBackdrop = event.target.closest('#model-modal');
            if (modalBackdrop && event.target === modalBackdrop) {
                closeModelModal();
                return;
            }

            const profileSaveBtn = event.target.closest('button[data-model-profile-save]');
            if (profileSaveBtn) {
                const modelId = profileSaveBtn.getAttribute('data-model-profile-save');
                if (!modelId) return;
                setModelProfileStatus('Saving profile...');
                saveModelProfile(modelId, false)
                    .then(async () => {
                        setModelProfileStatus('Profile saved.');
                        await refreshModels(true);
                        await loadModelProfileEditor(modelId);
                        announce(`Profile saved for ${modelId}.`);
                    })
                    .catch((e) => {
                        setModelProfileStatus(`Save failed: ${e.message}`, true);
                    });
                return;
            }

            const profileApplyBtn = event.target.closest('button[data-model-profile-apply]');
            if (profileApplyBtn) {
                const modelId = profileApplyBtn.getAttribute('data-model-profile-apply');
                if (!modelId) return;
                setModelProfileStatus('Saving and loading model...');
                saveModelProfile(modelId, true)
                    .then(async () => {
                        setModelProfileStatus('Profile applied and model load requested.');
                        await refreshModels(true);
                        await loadModelProfileEditor(modelId);
                        announce(`Profile applied for ${modelId}.`);
                    })
                    .catch((e) => {
                        setModelProfileStatus(`Apply failed: ${e.message}`, true);
                    });
                return;
            }

            const profileResetBtn = event.target.closest('button[data-model-profile-reset]');
            if (profileResetBtn) {
                const modelId = profileResetBtn.getAttribute('data-model-profile-reset');
                if (!modelId) return;
                setModelProfileStatus('Resetting profile...');
                resetModelProfile(modelId)
                    .then(async () => {
                        setModelProfileStatus('Profile reset.');
                        await refreshModels(true);
                        await loadModelProfileEditor(modelId);
                        announce(`Profile reset for ${modelId}.`);
                    })
                    .catch((e) => {
                        setModelProfileStatus(`Reset failed: ${e.message}`, true);
                    });
                return;
            }

            const backendCard = event.target.closest('.backend-card[data-backend]');
            if (backendCard) {
                const backendName = backendCard.getAttribute('data-backend');
                if (backendName) {
                    selectBackend(backendName, true);
                }
                return;
            }

            const modelInfoBtn = event.target.closest('button[data-model-info]');
            if (modelInfoBtn) {
                const modelId = modelInfoBtn.getAttribute('data-model-info');
                if (modelId) {
                    renderModelModal(modelId, true);
                }
                return;
            }

            const btn = event.target.closest('button[data-model-action]');
            if (!btn) return;
            const action = btn.getAttribute('data-model-action');
            const modelId = btn.getAttribute('data-model-id');
            if (!action || !modelId) return;
            runModelAction(action, modelId);
        });

        document.addEventListener('keydown', (event) => {
            if (event.key === 'Escape') {
                if (activeBottomSheet) { closeAllBottomSheets(); return; }
                closeModelModal();
                return;
            }
            const backendCard = event.target.closest('.backend-card[data-backend]');
            if (!backendCard) return;
            if (event.key !== 'Enter' && event.key !== ' ') return;
            event.preventDefault();
            const backendName = backendCard.getAttribute('data-backend');
            if (backendName) {
                selectBackend(backendName, true);
            }
        });

        document.getElementById('refresh-models-btn').addEventListener('click', () => {
            refreshModels();
        });

        initializeBackendSelector();
        initTerminalControls();
        initMobileFilter();
        connectTerminalFeed();
        setInterval(updateUptime, 1000);
        setInterval(refreshHealth, 30000);
        setInterval(refreshModels, 10000);
        setTimeout(refreshHealth, 100);
        setTimeout(refreshModels, 200);

        /* --- Bottom sheet backdrop + drag-to-dismiss --- */
        document.getElementById('bs-backdrop')?.addEventListener('click', closeAllBottomSheets);
        document.querySelectorAll('[data-bs-handle]').forEach(handle => {
            handle.addEventListener('touchstart', (e) => {
                bsDragStartY = e.touches[0].clientY;
                bsDragging = true;
                const sheet = handle.closest('.bottom-sheet');
                if (sheet) sheet.style.transition = 'none';
            }, { passive: true });
        });
        document.addEventListener('touchmove', (e) => {
            if (!bsDragging) return;
            bsDragCurrentY = e.touches[0].clientY;
            const dy = Math.max(0, bsDragCurrentY - bsDragStartY);
            const sheet = document.querySelector('.bottom-sheet.open');
            if (sheet) sheet.style.transform = 'translateY(' + dy + 'px)';
        }, { passive: true });
        document.addEventListener('touchend', () => {
            if (!bsDragging) return;
            bsDragging = false;
            const dy = bsDragCurrentY - bsDragStartY;
            const sheet = document.querySelector('.bottom-sheet.open');
            if (sheet) {
                sheet.style.transition = '';
                sheet.style.transform = '';
                if (dy > 80) closeAllBottomSheets();
            }
        });

        /* --- Mobile init + resize --- */
        renderMobileHealthChips();
        updateMobileModelBar();
        let mobileResizeTimer;
        window.addEventListener('resize', () => {
            clearTimeout(mobileResizeTimer);
            mobileResizeTimer = setTimeout(() => {
                renderMobileHealthChips();
                updateMobileModelBar();
                if (!isMobile()) closeAllBottomSheets();
            }, 250);
        });
    </script>
</body>
</html>"""


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
        _DASHBOARD_HTML
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
