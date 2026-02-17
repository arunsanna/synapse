"""LLM proxy routes — embeddings, chat completions, and model management."""

import asyncio
import json
import logging
import re
import threading
import time
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

from .backend_client import client
from .config import get_backend_url

router = APIRouter(tags=["llm"])
logger = logging.getLogger(__name__)

GENERAL_CHAT_MODEL = "Qwen3-8B-Q4_K_M"
CODER_CHAT_MODEL = "Qwen2.5-Coder-7B-Instruct-Q4_K_M"
AUTO_MODEL_ALIASES = {"", "auto", "synapse:auto", "synapse-auto"}
LOAD_TIMEOUT_SECONDS = 240.0
LOAD_POLL_INTERVAL_SECONDS = 1.0
MODEL_LOAD_DEFAULTS: dict[str, dict[str, Any]] = {}
MODEL_LOAD_DEFAULTS_LOCK = threading.Lock()

CODING_HINT_RE = re.compile(
    r"\b("
    r"code|python|javascript|typescript|java|go|rust|sql|regex|debug|"
    r"stack trace|exception|compile|refactor|unit test|algorithm|"
    r"function|class|api|dockerfile|kubernetes|yaml|json|bash|shell|git|"
    r"pull request|bug"
    r")\b",
    re.IGNORECASE,
)


def _get_config():
    from .main import get_backends_config
    return get_backends_config()


def _require_backend_url(config: dict, backend_name: str) -> str:
    """Resolve backend URL or raise 503 if backend is not configured."""
    try:
        return get_backend_url(config, backend_name)
    except KeyError as e:
        raise HTTPException(
            status_code=503,
            detail=f"Backend '{backend_name}' is not configured",
        ) from e


def _proxy_response(resp) -> Response:
    """Return backend response bytes while preserving content-type and status."""
    return Response(
        content=resp.content,
        status_code=resp.status_code,
        headers={"Content-Type": resp.headers.get("content-type", "application/json")},
    )


def _extract_user_text(messages: list) -> str:
    """Extract user text from OpenAI-style chat messages."""
    parts: list[str] = []
    for msg in messages:
        if not isinstance(msg, dict) or msg.get("role") != "user":
            continue
        content = msg.get("content")
        if isinstance(content, str):
            parts.append(content)
            continue
        if isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "text" and isinstance(item.get("content"), str):
                    parts.append(item["content"])
    return "\n".join(parts)


def _select_chat_model(payload: dict) -> tuple[str, str]:
    """Select model based on explicit request or a tiny coding-vs-general policy."""
    requested = payload.get("model")
    if isinstance(requested, str):
        req = requested.strip()
        if req.lower() not in AUTO_MODEL_ALIASES:
            return req, "explicit"

    messages = payload.get("messages", [])
    text = _extract_user_text(messages if isinstance(messages, list) else [])
    if CODING_HINT_RE.search(text):
        return CODER_CHAT_MODEL, "policy-coder"
    return GENERAL_CHAT_MODEL, "policy-general"


def _find_model(models: list, model_id: str) -> dict | None:
    for model in models:
        if isinstance(model, dict) and model.get("id") == model_id:
            return model
    return None


def _status_value(model: dict | None) -> str:
    if not model:
        return "missing"
    status = model.get("status")
    if isinstance(status, dict):
        value = status.get("value")
        if isinstance(value, str):
            return value
    return "unknown"


def _status_failed(model: dict | None) -> bool:
    if not model:
        return False
    status = model.get("status")
    return bool(isinstance(status, dict) and status.get("failed"))


def _get_model_load_defaults(model_id: str) -> dict[str, Any]:
    with MODEL_LOAD_DEFAULTS_LOCK:
        defaults = MODEL_LOAD_DEFAULTS.get(model_id, {})
        return dict(defaults)


def _update_model_load_defaults(model_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    with MODEL_LOAD_DEFAULTS_LOCK:
        current = dict(MODEL_LOAD_DEFAULTS.get(model_id, {}))
        for key, value in updates.items():
            if value is None:
                current.pop(key, None)
            else:
                current[key] = value
        if current:
            MODEL_LOAD_DEFAULTS[model_id] = current
        else:
            MODEL_LOAD_DEFAULTS.pop(model_id, None)
        return dict(current)


def _extract_model_and_load_defaults(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    model_id = payload.get("model")
    if not isinstance(model_id, str) or not model_id.strip():
        raise HTTPException(status_code=400, detail="'model' is required and must be a non-empty string")
    model_id = model_id.strip()

    updates: dict[str, Any] = {}

    if "temperature" in payload:
        value = payload.get("temperature")
        if value is None:
            updates["temperature"] = None
        else:
            try:
                numeric = float(value)
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=400, detail="'temperature' must be a number") from e
            if numeric < 0:
                raise HTTPException(status_code=400, detail="'temperature' must be >= 0")
            updates["temperature"] = numeric

    if "top_p" in payload:
        value = payload.get("top_p")
        if value is None:
            updates["top_p"] = None
        else:
            try:
                numeric = float(value)
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=400, detail="'top_p' must be a number") from e
            if numeric < 0 or numeric > 1:
                raise HTTPException(status_code=400, detail="'top_p' must be between 0 and 1")
            updates["top_p"] = numeric

    if "top_k" in payload:
        value = payload.get("top_k")
        if value is None:
            updates["top_k"] = None
        else:
            if isinstance(value, bool):
                raise HTTPException(status_code=400, detail="'top_k' must be an integer")
            try:
                numeric = int(value)
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=400, detail="'top_k' must be an integer") from e
            if numeric < 1:
                raise HTTPException(status_code=400, detail="'top_k' must be >= 1")
            updates["top_k"] = numeric

    if "system_prompt" in payload:
        value = payload.get("system_prompt")
        if value is None:
            updates["system_prompt"] = None
        else:
            if not isinstance(value, str):
                raise HTTPException(status_code=400, detail="'system_prompt' must be a string")
            prompt = value.strip()
            updates["system_prompt"] = prompt if prompt else None

    return model_id, updates


def _attach_model_load_defaults(models: list[dict]) -> None:
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str):
            continue
        defaults = _get_model_load_defaults(model_id)
        if not defaults:
            continue
        status = model.get("status")
        if not isinstance(status, dict):
            status = {}
            model["status"] = status
        status["synapse_defaults"] = defaults


def _apply_model_load_defaults_to_payload(payload: dict[str, Any], model_id: str) -> list[str]:
    defaults = _get_model_load_defaults(model_id)
    if not defaults:
        return []

    applied: list[str] = []
    for key in ("temperature", "top_p", "top_k"):
        if key in defaults and key not in payload:
            payload[key] = defaults[key]
            applied.append(key)

    system_prompt = defaults.get("system_prompt")
    if isinstance(system_prompt, str) and system_prompt:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            messages = []
            payload["messages"] = messages

        has_system_message = any(
            isinstance(message, dict) and message.get("role") == "system"
            for message in messages
        )
        if not has_system_message:
            messages.insert(0, {"role": "system", "content": system_prompt})
            applied.append("system_prompt")

    return applied


async def _list_router_models(router_url: str) -> list[dict]:
    resp = await client.request(
        "llama-router",
        "GET",
        f"{router_url}/models",
        timeout_type="default",
    )
    if resp.status_code != 200:
        raise HTTPException(
            status_code=502,
            detail=f"llama-router /models failed with status {resp.status_code}",
        )
    data = resp.json()
    models = data.get("data", [])
    if not isinstance(models, list):
        raise HTTPException(status_code=502, detail="llama-router /models returned invalid payload")
    return models


async def _ensure_router_model_loaded(router_url: str, model_id: str) -> None:
    """Ensure selected model is loaded; unload other loaded models when needed."""
    models = await _list_router_models(router_url)
    model = _find_model(models, model_id)
    if model is None:
        raise HTTPException(
            status_code=400,
            detail=f"Model '{model_id}' is not available in llama-router /models",
        )

    loaded_other_models = [
        m.get("id") for m in models
        if isinstance(m, dict)
        and m.get("id") != model_id
        and _status_value(m) == "loaded"
        and isinstance(m.get("id"), str)
    ]
    for other_id in loaded_other_models:
        logger.info("Unloading active model '%s' before loading '%s'", other_id, model_id)
        await client.request(
            "llama-router",
            "POST",
            f"{router_url}/models/unload",
            json={"model": other_id},
            timeout_type="llm",
        )

    state = _status_value(model)
    if state == "loaded":
        return

    if state != "loading":
        load_resp = await client.request(
            "llama-router",
            "POST",
            f"{router_url}/models/load",
            json={"model": model_id},
            timeout_type="llm",
        )
        if load_resp.status_code != 200:
            raise HTTPException(
                status_code=502,
                detail=f"llama-router failed to load '{model_id}' (status {load_resp.status_code})",
            )

    deadline = time.monotonic() + LOAD_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        await asyncio.sleep(LOAD_POLL_INTERVAL_SECONDS)
        models = await _list_router_models(router_url)
        model = _find_model(models, model_id)
        state = _status_value(model)
        if state == "loaded":
            return
        if _status_failed(model):
            raise HTTPException(
                status_code=502,
                detail=f"llama-router reported failed load for '{model_id}'",
            )

    raise HTTPException(
        status_code=504,
        detail=f"Timed out waiting for model '{model_id}' to load",
    )


@router.post("/v1/embeddings")
async def embeddings(request: Request):
    """Proxy embeddings to llama-embed."""
    config = _get_config()
    backend_url = _require_backend_url(config, "llama-embed")
    body = await request.body()

    url = f"{backend_url}/v1/embeddings"
    resp = await client.request(
        "llama-embed", "POST", url,
        content=body,
        headers={"Content-Type": "application/json"},
        timeout_type="embeddings",
    )
    return JSONResponse(content=resp.json(), status_code=resp.status_code)


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """Proxy OpenAI-compatible chat completions to llama.cpp router backend."""
    config = _get_config()
    router_url = _require_backend_url(config, "llama-router")
    body = await request.body()

    if not body:
        raise HTTPException(status_code=400, detail="Request body is required")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e.msg}") from e
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    selected_model, reason = _select_chat_model(payload)
    payload["model"] = selected_model
    applied_defaults = _apply_model_load_defaults_to_payload(payload, selected_model)
    logger.info(
        "Chat model selection -> model=%s reason=%s defaults=%s",
        selected_model,
        reason,
        ",".join(applied_defaults) if applied_defaults else "none",
    )

    await _ensure_router_model_loaded(router_url, selected_model)

    # Handle streaming explicitly to keep SSE chunking end-to-end.
    stream = bool(payload.get("stream", False))
    proxy_body = json.dumps(payload).encode("utf-8")

    url = f"{router_url}/v1/chat/completions"
    if stream:
        return StreamingResponse(
            client.stream_bytes(
                "llama-router",
                "POST",
                url,
                content=proxy_body,
                headers={"Content-Type": "application/json"},
                timeout_type="llm",
            ),
            media_type="text/event-stream",
        )

    resp = await client.request(
        "llama-router",
        "POST",
        url,
        content=proxy_body,
        headers={"Content-Type": "application/json"},
        timeout_type="llm",
    )
    return _proxy_response(resp)


@router.get("/models")
async def list_router_models():
    """List llama.cpp router models and their load status."""
    config = _get_config()
    backend_url = _require_backend_url(config, "llama-router")
    resp = await client.request(
        "llama-router",
        "GET",
        f"{backend_url}/models",
        timeout_type="default",
    )
    if resp.status_code != 200:
        return _proxy_response(resp)
    try:
        data = resp.json()
    except ValueError:
        return _proxy_response(resp)
    if isinstance(data, dict):
        models = data.get("data")
        if isinstance(models, list):
            _attach_model_load_defaults(models)
        return JSONResponse(content=data, status_code=resp.status_code)
    return _proxy_response(resp)


@router.post("/models/load")
async def load_router_model(request: Request):
    """Load a model in llama.cpp router mode."""
    config = _get_config()
    backend_url = _require_backend_url(config, "llama-router")
    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="Request body is required")
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e.msg}") from e
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")

    model_id, updates = _extract_model_and_load_defaults(payload)
    active_defaults = _get_model_load_defaults(model_id)
    if updates:
        active_defaults = _update_model_load_defaults(model_id, updates)

    resp = await client.request(
        "llama-router",
        "POST",
        f"{backend_url}/models/load",
        json={"model": model_id},
        timeout_type="llm",
    )
    if resp.status_code != 200:
        return _proxy_response(resp)
    try:
        data = resp.json()
    except ValueError:
        return _proxy_response(resp)
    if isinstance(data, dict):
        if active_defaults:
            data["synapse_defaults"] = active_defaults
        return JSONResponse(content=data, status_code=resp.status_code)
    return _proxy_response(resp)


@router.post("/models/unload")
async def unload_router_model(request: Request):
    """Unload a model in llama.cpp router mode."""
    config = _get_config()
    backend_url = _require_backend_url(config, "llama-router")
    body = await request.body()
    resp = await client.request(
        "llama-router",
        "POST",
        f"{backend_url}/models/unload",
        content=body,
        headers={"Content-Type": "application/json"},
        timeout_type="llm",
    )
    return _proxy_response(resp)


@router.get("/v1/models")
async def list_models():
    """Aggregate model lists from all LLM backends."""
    config = _get_config()
    models = []

    # llama-embed models
    try:
        embed_url = get_backend_url(config, "llama-embed")
        resp = await client.request(
            "llama-embed", "GET", f"{embed_url}/v1/models",
            timeout_type="default",
        )
        if resp.status_code == 200:
            data = resp.json()
            models_from_router = data.get("data", [])
            if isinstance(models_from_router, list):
                _attach_model_load_defaults(models_from_router)
            for m in models_from_router:
                models.append(m)
    except Exception as e:
        logger.warning("Failed to list llama-embed models: %s", e)

    # llama-router models (chat/completions)
    try:
        router_url = get_backend_url(config, "llama-router")
        resp = await client.request(
            "llama-router", "GET", f"{router_url}/v1/models",
            timeout_type="default",
        )
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("data", []):
                models.append(m)
    except KeyError:
        pass
    except Exception as e:
        logger.warning("Failed to list llama-router models: %s", e)

    # vLLM models (when deployed — currently commented out in backends.yaml)
    try:
        vllm_url = get_backend_url(config, "vllm")
        resp = await client.request(
            "vllm", "GET", f"{vllm_url}/v1/models",
            timeout_type="default",
        )
        if resp.status_code == 200:
            data = resp.json()
            for m in data.get("data", []):
                models.append(m)
    except KeyError:
        pass  # vLLM not configured yet
    except Exception as e:
        logger.warning("Failed to list vLLM models: %s", e)

    return {"object": "list", "data": models}
