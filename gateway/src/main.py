"""Synapse Gateway — unified AI proxy for ArunLabs Forge cluster."""

import html
import logging
import time as _time
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

from .backend_client import client
from .config import load_backends_config, settings
from .voice_manager import VoiceManager

logger = logging.getLogger(__name__)

# Shared state populated at startup
_backends_config: dict = {}
_voice_manager: VoiceManager | None = None
_start_time: float = 0.0


def get_backends_config() -> dict:
    return _backends_config


def get_voice_manager() -> VoiceManager:
    return _voice_manager


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup: load config, init httpx pool, init voice manager."""
    global _backends_config, _voice_manager, _start_time

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
    await client.start()
    _start_time = _time.time()
    logger.info("Synapse Gateway started")

    yield

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
            f'<div class="backend-card" data-backend="{html.escape(name)}">'
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
        *, *::before, *::after { box-sizing: border-box; margin: 0; padding: 0; }
        body {
            background: #0f172a; color: #f1f5f9;
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            line-height: 1.6; padding: 2rem; max-width: 1200px; margin: 0 auto;
        }
        a { color: #3b82f6; text-decoration: none; }
        a:hover { text-decoration: underline; }
        code, .mono {
            font-family: 'SF Mono', 'Cascadia Code', 'Fira Code', Consolas, monospace;
        }
        .header {
            margin-bottom: 2rem; padding-bottom: 1.5rem;
            border-bottom: 1px solid #334155;
        }
        .header h1 {
            font-size: 1.75rem; font-weight: 700;
            display: flex; align-items: center; gap: 0.75rem;
        }
        .hdr-dot {
            width: 12px; height: 12px; border-radius: 50%;
            display: inline-block;
        }
        .hdr-dot.healthy { background: #22c55e; box-shadow: 0 0 8px #22c55e40; }
        .hdr-dot.degraded { background: #eab308; box-shadow: 0 0 8px #eab30840; }
        .header-meta {
            display: flex; gap: 2rem; flex-wrap: wrap;
            color: #94a3b8; font-size: 0.875rem; margin-top: 0.5rem;
        }
        .header-meta code { color: #3b82f6; }
        .panel {
            background: #1e293b; border: 1px solid #334155;
            border-radius: 0.75rem; padding: 1.5rem; margin-bottom: 1.5rem;
        }
        .panel-title {
            font-size: 1rem; font-weight: 600; color: #94a3b8;
            text-transform: uppercase; letter-spacing: 0.05em;
            margin-bottom: 1rem; display: flex;
            align-items: center; justify-content: space-between;
        }
        .panel-title .refresh-info {
            font-size: 0.75rem; font-weight: 400;
            text-transform: none; letter-spacing: normal; color: #64748b;
        }
        .backends-grid {
            display: grid;
            grid-template-columns: repeat(auto-fill, minmax(220px, 1fr));
            gap: 0.75rem;
        }
        .backend-card {
            background: #0f172a; border: 1px solid #334155;
            border-radius: 0.5rem; padding: 1rem;
        }
        .backend-name {
            font-weight: 600; font-size: 0.9rem;
            display: flex; align-items: center; gap: 0.5rem;
        }
        .status-dot {
            width: 8px; height: 8px; border-radius: 50%;
            display: inline-block; flex-shrink: 0;
        }
        .status-dot.healthy { background: #22c55e; box-shadow: 0 0 6px #22c55e60; }
        .status-dot.unhealthy,
        .status-dot.unreachable { background: #ef4444; box-shadow: 0 0 6px #ef444460; }
        .status-dot.degraded { background: #eab308; box-shadow: 0 0 6px #eab30860; }
        .status-dot.checking { background: #64748b; }
        .backend-status { font-size: 0.8rem; color: #94a3b8; margin-top: 0.25rem; }
        .backend-health-url {
            font-family: 'SF Mono', Consolas, monospace;
            font-size: 0.7rem; color: #64748b; margin-top: 0.25rem;
        }
        .table-wrap { overflow-x: auto; }
        table { width: 100%; border-collapse: collapse; font-size: 0.875rem; }
        th {
            text-align: left; padding: 0.625rem 0.75rem;
            background: #0f172a; color: #94a3b8; font-weight: 600;
            font-size: 0.8rem; text-transform: uppercase;
            letter-spacing: 0.03em; border-bottom: 1px solid #334155;
        }
        td {
            padding: 0.625rem 0.75rem; border-bottom: 1px solid #1e293b;
            vertical-align: top;
        }
        td code { font-size: 0.8125rem; color: #3b82f6; }
        .cap-tag {
            display: inline-block; padding: 0.125rem 0.5rem;
            background: #334155; border-radius: 9999px;
            font-size: 0.75rem; color: #94a3b8;
            margin: 0.125rem 0.25rem 0.125rem 0;
        }
        .device-badge {
            display: inline-block; padding: 0.125rem 0.5rem;
            border-radius: 0.25rem; font-size: 0.75rem; font-weight: 600;
        }
        .device-badge.cpu { background: #1e3a5f; color: #60a5fa; }
        .endpoint-group { margin-bottom: 1.25rem; }
        .endpoint-group:last-child { margin-bottom: 0; }
        .endpoint-group-title {
            font-size: 0.75rem; font-weight: 600; color: #64748b;
            text-transform: uppercase; letter-spacing: 0.05em;
            margin-bottom: 0.375rem; padding-bottom: 0.25rem;
            border-bottom: 1px solid #334155;
        }
        .endpoint-row {
            font-family: 'SF Mono', Consolas, monospace;
            font-size: 0.8125rem; padding: 0.25rem 0;
            display: flex; gap: 0.75rem;
        }
        .ep-method { font-weight: 700; min-width: 3.5rem; text-align: right; }
        .ep-method.get { color: #22c55e; }
        .ep-method.post { color: #3b82f6; }
        .ep-method.delete { color: #ef4444; }
        .ep-path { color: #e2e8f0; }
        .quick-links { display: flex; gap: 0.5rem; flex-wrap: wrap; }
        .quick-links a {
            display: inline-block; padding: 0.5rem 1rem;
            background: #0f172a; color: #3b82f6;
            border: 1px solid #334155; border-radius: 0.375rem;
            font-family: 'SF Mono', Consolas, monospace;
            font-size: 0.8125rem; transition: border-color 0.15s, background 0.15s;
        }
        .quick-links a:hover {
            border-color: #3b82f6; background: #1e293b; text-decoration: none;
        }
        @media (max-width: 640px) {
            body { padding: 1rem; }
            .header-meta { flex-direction: column; gap: 0.5rem; }
            .backends-grid { grid-template-columns: 1fr; }
            .endpoint-row { font-size: 0.75rem; }
        }
    </style>
</head>
<body>
    <div class="header">
        <h1>
            <span id="overall-dot" class="hdr-dot __OVERALL_STATUS_CLASS__"></span>
            Synapse Gateway
        </h1>
        <div class="header-meta">
            <span>Base URL: <code>synapse.arunlabs.com</code></span>
            <span>Uptime: <code id="uptime">__UPTIME__</code></span>
            <span>Backends: <code>__BACKEND_COUNT__</code></span>
        </div>
    </div>

    <div class="panel">
        <div class="panel-title">
            Backend Health
            <span class="refresh-info" id="refresh-info">Auto-refreshes every 30s</span>
        </div>
        <div class="backends-grid" id="backends-grid">__BACKEND_CARDS__</div>
    </div>

    <div class="panel">
        <div class="panel-title">Models &amp; Capabilities</div>
        <div class="table-wrap">
            <table>
                <thead>
                    <tr>
                        <th>Backend</th><th>Model</th>
                        <th>Device</th><th>Capabilities</th>
                    </tr>
                </thead>
                <tbody>
                    <tr>
                        <td><code>llama-embed</code></td>
                        <td>snowflake-arctic-embed2</td>
                        <td><span class="device-badge cpu">CPU</span></td>
                        <td>
                            <span class="cap-tag">Text embeddings</span>
                            <span class="cap-tag">1024 dims</span>
                        </td>
                    </tr>
                    <tr>
                        <td><code>chatterbox-tts</code></td>
                        <td>Chatterbox Turbo (350M)</td>
                        <td><span class="device-badge cpu">CPU</span></td>
                        <td>
                            <span class="cap-tag">Voice cloning</span>
                            <span class="cap-tag">23 languages</span>
                        </td>
                    </tr>
                    <tr>
                        <td><code>whisper-stt</code></td>
                        <td>Whisper large-v3-turbo (int8)</td>
                        <td><span class="device-badge cpu">CPU</span></td>
                        <td>
                            <span class="cap-tag">Transcription</span>
                            <span class="cap-tag">Language detection</span>
                            <span class="cap-tag">Streaming</span>
                        </td>
                    </tr>
                    <tr>
                        <td><code>pyannote-speaker</code></td>
                        <td>pyannote 3.1</td>
                        <td><span class="device-badge cpu">CPU</span></td>
                        <td>
                            <span class="cap-tag">Diarization</span>
                            <span class="cap-tag">Speaker verification</span>
                        </td>
                    </tr>
                    <tr>
                        <td><code>deepfilter-audio</code></td>
                        <td>DeepFilterNet3 + ffmpeg</td>
                        <td><span class="device-badge cpu">CPU</span></td>
                        <td>
                            <span class="cap-tag">Noise reduction</span>
                            <span class="cap-tag">Format conversion</span>
                        </td>
                    </tr>
                </tbody>
            </table>
        </div>
    </div>

    <div class="panel">
        <div class="panel-title">
            API Endpoints
            <span style="font-weight:400;font-size:0.8rem;color:#64748b">17 total</span>
        </div>

        <div class="endpoint-group">
            <div class="endpoint-group-title">Health</div>
            <div class="endpoint-row">
                <span class="ep-method get">GET</span>
                <span class="ep-path">/health</span>
            </div>
        </div>

        <div class="endpoint-group">
            <div class="endpoint-group-title">Voice Management</div>
            <div class="endpoint-row">
                <span class="ep-method get">GET</span>
                <span class="ep-path">/voices</span>
            </div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/voices</span>
            </div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/voices/{id}/references</span>
            </div>
            <div class="endpoint-row">
                <span class="ep-method delete">DELETE</span>
                <span class="ep-path">/voices/{id}</span>
            </div>
        </div>

        <div class="endpoint-group">
            <div class="endpoint-group-title">Text-to-Speech</div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/tts/synthesize</span>
            </div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/tts/stream</span>
            </div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/tts/interpolate</span>
            </div>
            <div class="endpoint-row">
                <span class="ep-method get">GET</span>
                <span class="ep-path">/tts/languages</span>
            </div>
        </div>

        <div class="endpoint-group">
            <div class="endpoint-group-title">Speech-to-Text</div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/stt/transcribe</span>
            </div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/stt/detect-language</span>
            </div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/stt/stream</span>
            </div>
        </div>

        <div class="endpoint-group">
            <div class="endpoint-group-title">Speaker Analysis</div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/speakers/diarize</span>
            </div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/speakers/verify</span>
            </div>
        </div>

        <div class="endpoint-group">
            <div class="endpoint-group-title">Audio Processing</div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/audio/denoise</span>
            </div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/audio/convert</span>
            </div>
        </div>

        <div class="endpoint-group">
            <div class="endpoint-group-title">Embeddings</div>
            <div class="endpoint-row">
                <span class="ep-method post">POST</span>
                <span class="ep-path">/v1/embeddings</span>
            </div>
        </div>
    </div>

    <div class="panel">
        <div class="panel-title">Quick Links</div>
        <div class="quick-links">
            <a href="/docs">Swagger UI</a>
            <a href="/redoc">ReDoc</a>
            <a href="/openapi.json">OpenAPI Spec</a>
            <a href="/health">Health (JSON)</a>
        </div>
    </div>

    <script>
        let uptimeSeconds = __UPTIME_SECONDS__;
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
            try {
                const resp = await fetch('/health');
                const data = await resp.json();
                const dot = document.getElementById('overall-dot');
                dot.className = 'hdr-dot ' + (data.status === 'healthy' ? 'healthy' : 'degraded');
                const backends = data.backends || {};
                for (const [name, info] of Object.entries(backends)) {
                    const card = document.querySelector('[data-backend="' + name + '"]');
                    if (!card) continue;
                    const sd = card.querySelector('.status-dot');
                    const st = card.querySelector('.backend-status');
                    const status = info.status || 'unreachable';
                    sd.className = 'status-dot ' + status;
                    let label = status.charAt(0).toUpperCase() + status.slice(1);
                    if (info.code) label += ' (' + info.code + ')';
                    if (info.error) label += ' \u2014 ' + info.error.substring(0, 60);
                    st.textContent = label;
                }
                const now = new Date();
                document.getElementById('refresh-info').textContent =
                    'Last checked: ' + now.toLocaleTimeString() + ' \u00b7 refreshes every 30s';
            } catch (e) {
                document.getElementById('refresh-info').textContent =
                    'Refresh failed \u2014 retrying in 30s';
            }
        }
        setInterval(updateUptime, 1000);
        setInterval(refreshHealth, 30000);
        setTimeout(refreshHealth, 100);
    </script>
</body>
</html>"""


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard():
    """Self-contained HTML status dashboard with live health monitoring."""
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
    )


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
