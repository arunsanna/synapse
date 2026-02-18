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
from .config import get_backend_url, settings
from .model_profile_store import ModelProfileStore

router = APIRouter(tags=["llm"])
logger = logging.getLogger(__name__)

GENERAL_CHAT_MODEL = "Qwen3-8B-Q4_K_M"
CODER_CHAT_MODEL = "Qwen2.5-Coder-7B-Instruct-Q4_K_M"
AUTO_MODEL_ALIASES = {"", "auto", "synapse:auto", "synapse-auto"}
LOAD_TIMEOUT_SECONDS = 240.0
LOAD_POLL_INTERVAL_SECONDS = 1.0
MODEL_PROFILE_STORE = ModelProfileStore(settings.model_profiles_path)
MODEL_PROFILE_LOCK = threading.Lock()
SPLIT_MODEL_ID_RE = re.compile(r"^(?P<base>.+)-(?P<part>\d+)-of-(?P<total>\d+)$")
REASONING_LINE_RE = re.compile(r"(?im)^\s*reasoning\s*:\s*(low|medium|high)\s*$")

BASE_PROFILE_FIELDS: list[dict[str, Any]] = [
    {
        "name": "system_prompt",
        "label": "System Prompt",
        "type": "string",
        "default": "",
        "applies_at": "generation",
        "description": "Default system prompt prepended when request has no system message.",
    },
    {
        "name": "temperature",
        "label": "Temperature",
        "type": "number",
        "min": 0.0,
        "max": 2.0,
        "step": 0.01,
        "default": 1.0,
        "applies_at": "generation",
        "description": "Higher values increase randomness, lower values make outputs more deterministic.",
    },
    {
        "name": "top_p",
        "label": "Top P",
        "type": "number",
        "min": 0.0,
        "max": 1.0,
        "step": 0.01,
        "default": 0.95,
        "applies_at": "generation",
        "description": "Nucleus sampling cutoff. Lower values constrain token choices.",
    },
    {
        "name": "top_k",
        "label": "Top K",
        "type": "integer",
        "min": 1,
        "max": 1000,
        "step": 1,
        "default": 40,
        "applies_at": "generation",
        "description": "Samples only from the top-K candidate tokens each step.",
    },
    {
        "name": "min_p",
        "label": "Min P",
        "type": "number",
        "min": 0.0,
        "max": 1.0,
        "step": 0.01,
        "default": 0.01,
        "applies_at": "generation",
        "description": "Minimum token probability floor; useful for llama.cpp where default is often too strict.",
    },
    {
        "name": "repeat_penalty",
        "label": "Repeat Penalty",
        "type": "number",
        "min": 0.0,
        "max": 3.0,
        "step": 0.01,
        "default": 1.0,
        "applies_at": "generation",
        "description": "Penalizes repeated tokens. 1.0 disables repetition penalty.",
    },
    {
        "name": "max_tokens",
        "label": "Max Tokens",
        "type": "integer",
        "min": 1,
        "max": 32768,
        "step": 1,
        "default": 1024,
        "applies_at": "generation",
        "description": "Default maximum number of completion tokens per request.",
    },
]

GPT_OSS_PROFILE_FIELDS: list[dict[str, Any]] = [
    {
        "name": "reasoning_effort",
        "label": "Reasoning Effort",
        "type": "enum",
        "choices": ["low", "medium", "high"],
        "default": "high",
        "applies_at": "generation",
        "description": "Injected as 'Reasoning: <level>' in system instructions when absent.",
    }
]

PROFILE_DEFAULT_APPLY_KEYS = (
    "temperature",
    "top_p",
    "top_k",
    "min_p",
    "repeat_penalty",
    "max_tokens",
)

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


def _parse_split_model_id(model_id: str) -> tuple[str, int, int] | None:
    match = SPLIT_MODEL_ID_RE.match(model_id)
    if not match:
        return None
    base = match.group("base")
    try:
        part = int(match.group("part"))
        total = int(match.group("total"))
    except ValueError:
        return None
    if part < 1 or total < 2 or part > total:
        return None
    return base, part, total


def _collapse_split_models(models: list[dict]) -> list[dict]:
    groups: dict[tuple[str, int], list[tuple[int, dict]]] = {}
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str):
            continue
        parsed = _parse_split_model_id(model_id)
        if not parsed:
            continue
        base, part, total = parsed
        groups.setdefault((base, total), []).append((part, model))

    if not groups:
        return models

    collapsed: list[dict] = []
    emitted: set[tuple[str, int]] = set()
    for model in models:
        if not isinstance(model, dict):
            collapsed.append(model)
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str):
            collapsed.append(model)
            continue
        parsed = _parse_split_model_id(model_id)
        if not parsed:
            collapsed.append(model)
            continue
        base, _part, total = parsed
        key = (base, total)
        if key in emitted:
            continue
        emitted.add(key)

        entries = groups.get(key, [])
        if not entries:
            collapsed.append(model)
            continue
        entries.sort(key=lambda item: item[0])
        primary = next((entry for part, entry in entries if part == 1), entries[0][1])
        merged = dict(primary)

        status = merged.get("status")
        if isinstance(status, dict):
            merged_status = dict(status)
            all_values = [
                entry.get("status", {}).get("value")
                for _, entry in entries
                if isinstance(entry.get("status"), dict)
            ]
            if any(value == "loaded" for value in all_values):
                merged_status["value"] = "loaded"
            elif any(value == "loading" for value in all_values):
                merged_status["value"] = "loading"
            elif any(value == "unloading" for value in all_values):
                merged_status["value"] = "unloading"
            elif all(value == "unloaded" for value in all_values if isinstance(value, str)):
                merged_status["value"] = "unloaded"
            if any(_status_failed(entry) for _, entry in entries):
                merged_status["failed"] = True
            merged["status"] = merged_status

        collapsed.append(merged)

    return collapsed


def _infer_model_family(model_id: str) -> str:
    model_lc = model_id.lower()
    if model_lc.startswith("gpt-oss-"):
        return "gpt-oss"
    if "glm-4.7-flash" in model_lc:
        return "glm-4.7-flash"
    if "qwen" in model_lc:
        return "qwen"
    return "generic"


def _profile_fields_for_model(model_id: str) -> list[dict[str, Any]]:
    fields = [dict(field) for field in BASE_PROFILE_FIELDS]
    if _infer_model_family(model_id) == "gpt-oss":
        fields.extend(dict(field) for field in GPT_OSS_PROFILE_FIELDS)
    return fields


def _profile_field_map(model_id: str) -> dict[str, dict[str, Any]]:
    return {field["name"]: field for field in _profile_fields_for_model(model_id)}


def _schema_payload(model_id: str) -> dict[str, Any]:
    fields = _profile_fields_for_model(model_id)
    return {
        "model": model_id,
        "family": _infer_model_family(model_id),
        "fields": fields,
        "notes": [
            "Generation settings are persisted per model and auto-applied when request values are missing.",
            "llama.cpp runtime load args (ctx-size, threads, batch, etc.) are currently read-only in Synapse.",
        ],
    }


def _normalize_profile_updates(model_id: str, raw_updates: dict[str, Any]) -> dict[str, Any]:
    field_map = _profile_field_map(model_id)
    normalized: dict[str, Any] = {}

    for key, value in raw_updates.items():
        spec = field_map.get(key)
        if spec is None:
            raise HTTPException(status_code=400, detail=f"Unknown profile field: '{key}'")

        if value is None:
            normalized[key] = None
            continue

        kind = spec.get("type")
        if kind == "number":
            try:
                parsed = float(value)
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=400, detail=f"'{key}' must be a number") from e
            min_value = spec.get("min")
            max_value = spec.get("max")
            if isinstance(min_value, (int, float)) and parsed < float(min_value):
                raise HTTPException(status_code=400, detail=f"'{key}' must be >= {min_value}")
            if isinstance(max_value, (int, float)) and parsed > float(max_value):
                raise HTTPException(status_code=400, detail=f"'{key}' must be <= {max_value}")
            normalized[key] = parsed
            continue

        if kind == "integer":
            if isinstance(value, bool):
                raise HTTPException(status_code=400, detail=f"'{key}' must be an integer")
            try:
                parsed = int(value)
            except (TypeError, ValueError) as e:
                raise HTTPException(status_code=400, detail=f"'{key}' must be an integer") from e
            min_value = spec.get("min")
            max_value = spec.get("max")
            if isinstance(min_value, int) and parsed < min_value:
                raise HTTPException(status_code=400, detail=f"'{key}' must be >= {min_value}")
            if isinstance(max_value, int) and parsed > max_value:
                raise HTTPException(status_code=400, detail=f"'{key}' must be <= {max_value}")
            normalized[key] = parsed
            continue

        if kind == "enum":
            if not isinstance(value, str):
                raise HTTPException(status_code=400, detail=f"'{key}' must be a string")
            parsed = value.strip().lower()
            choices = spec.get("choices", [])
            if parsed not in choices:
                raise HTTPException(status_code=400, detail=f"'{key}' must be one of: {', '.join(choices)}")
            normalized[key] = parsed
            continue

        if kind == "string":
            if not isinstance(value, str):
                raise HTTPException(status_code=400, detail=f"'{key}' must be a string")
            parsed = value.strip()
            normalized[key] = parsed if parsed else None
            continue

        raise HTTPException(status_code=400, detail=f"Unsupported field type for '{key}'")

    return normalized


def _get_model_profile(model_id: str) -> dict[str, Any]:
    with MODEL_PROFILE_LOCK:
        return MODEL_PROFILE_STORE.get_profile(model_id)


def _set_model_profile(model_id: str, values: dict[str, Any]) -> dict[str, Any]:
    with MODEL_PROFILE_LOCK:
        return MODEL_PROFILE_STORE.set_profile(model_id, values)


def _update_model_profile(model_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    with MODEL_PROFILE_LOCK:
        return MODEL_PROFILE_STORE.patch_profile(model_id, updates)


def _get_model_load_defaults(model_id: str) -> dict[str, Any]:
    return _get_model_profile(model_id)


def _update_model_load_defaults(model_id: str, updates: dict[str, Any]) -> dict[str, Any]:
    return _update_model_profile(model_id, updates)


def _extract_model_and_load_defaults(payload: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    model_id = payload.get("model")
    if not isinstance(model_id, str) or not model_id.strip():
        raise HTTPException(status_code=400, detail="'model' is required and must be a non-empty string")
    model_id = model_id.strip()
    updates = _normalize_profile_updates(
        model_id,
        {
            key: value
            for key, value in payload.items()
            if key != "model"
        },
    )
    return model_id, updates


def _attach_model_load_defaults(models: list[dict]) -> None:
    for model in models:
        if not isinstance(model, dict):
            continue
        model_id = model.get("id")
        if not isinstance(model_id, str):
            continue
        profile = _get_model_profile(model_id)
        if not profile:
            continue
        status = model.get("status")
        if not isinstance(status, dict):
            status = {}
            model["status"] = status
        status["synapse_profile"] = profile
        status["synapse_defaults"] = {
            key: value for key, value in profile.items()
            if key in PROFILE_DEFAULT_APPLY_KEYS or key in {"system_prompt", "reasoning_effort"}
        }


def _apply_model_load_defaults_to_payload(payload: dict[str, Any], model_id: str) -> list[str]:
    profile = _get_model_profile(model_id)
    if not profile:
        return []

    applied: list[str] = []
    for key in PROFILE_DEFAULT_APPLY_KEYS:
        if key in profile and key not in payload:
            payload[key] = profile[key]
            applied.append(key)

    system_prompt = profile.get("system_prompt")
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

    reasoning_effort = profile.get("reasoning_effort")
    if isinstance(reasoning_effort, str) and reasoning_effort in {"low", "medium", "high"}:
        messages = payload.get("messages")
        if not isinstance(messages, list):
            messages = []
            payload["messages"] = messages
        reasoning_line = f"Reasoning: {reasoning_effort}"
        system_messages = [
            message for message in messages
            if isinstance(message, dict) and message.get("role") == "system"
        ]
        if not system_messages:
            messages.insert(0, {"role": "system", "content": reasoning_line})
            applied.append("reasoning_effort")
        else:
            first_system = system_messages[0]
            content = first_system.get("content")
            if isinstance(content, str) and not REASONING_LINE_RE.search(content):
                first_system["content"] = f"{reasoning_line}\n\n{content}" if content else reasoning_line
                applied.append("reasoning_effort")

    return applied


def _parse_json_object(body: bytes, *, required: bool = True) -> dict[str, Any]:
    if not body:
        if required:
            raise HTTPException(status_code=400, detail="Request body is required")
        return {}
    try:
        payload = json.loads(body)
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON body: {e.msg}") from e
    if not isinstance(payload, dict):
        raise HTTPException(status_code=400, detail="JSON body must be an object")
    return payload


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
    payload = _parse_json_object(await request.body(), required=True)

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
            data["data"] = _collapse_split_models(models)
        return JSONResponse(content=data, status_code=resp.status_code)
    return _proxy_response(resp)


@router.get("/models/{model_id}/schema")
async def get_model_profile_schema(model_id: str):
    """Return editable profile schema and field help for a model."""
    config = _get_config()
    backend_url = _require_backend_url(config, "llama-router")
    models = await _list_router_models(backend_url)
    if _find_model(models, model_id) is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    return _schema_payload(model_id)


@router.get("/models/{model_id}/profile")
async def get_model_profile(model_id: str):
    """Return persisted profile values for a model."""
    config = _get_config()
    backend_url = _require_backend_url(config, "llama-router")
    models = await _list_router_models(backend_url)
    if _find_model(models, model_id) is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")
    return {
        "model": model_id,
        "family": _infer_model_family(model_id),
        "values": _get_model_profile(model_id),
    }


@router.put("/models/{model_id}/profile")
async def put_model_profile(model_id: str, request: Request):
    """Create or update persisted profile values for a model."""
    config = _get_config()
    backend_url = _require_backend_url(config, "llama-router")
    models = await _list_router_models(backend_url)
    if _find_model(models, model_id) is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

    payload = _parse_json_object(await request.body(), required=True)
    replace = False
    values_payload: dict[str, Any]
    if "values" in payload:
        values = payload.get("values")
        if not isinstance(values, dict):
            raise HTTPException(status_code=400, detail="'values' must be an object")
        values_payload = values
        replace = bool(payload.get("replace", False))
    else:
        replace = bool(payload.get("replace", False))
        values_payload = {k: v for k, v in payload.items() if k != "replace"}

    normalized = _normalize_profile_updates(model_id, values_payload)
    if replace:
        active_profile = _set_model_profile(
            model_id,
            {key: value for key, value in normalized.items() if value is not None},
        )
    else:
        active_profile = _update_model_profile(model_id, normalized)

    return {
        "success": True,
        "model": model_id,
        "family": _infer_model_family(model_id),
        "values": active_profile,
    }


@router.post("/models/{model_id}/profile/apply")
async def apply_model_profile(model_id: str, request: Request):
    """Apply persisted profile and optionally load the model."""
    config = _get_config()
    backend_url = _require_backend_url(config, "llama-router")
    models = await _list_router_models(backend_url)
    if _find_model(models, model_id) is None:
        raise HTTPException(status_code=404, detail=f"Model '{model_id}' not found")

    payload = _parse_json_object(await request.body(), required=False)
    load_model = bool(payload.get("load_model", False))
    load_status = {"requested": load_model, "success": True}
    if load_model:
        resp = await client.request(
            "llama-router",
            "POST",
            f"{backend_url}/models/load",
            json={"model": model_id},
            timeout_type="llm",
        )
        if resp.status_code != 200:
            load_status = {
                "requested": True,
                "success": False,
                "status_code": resp.status_code,
            }

    return {
        "success": bool(load_status.get("success", True)),
        "model": model_id,
        "family": _infer_model_family(model_id),
        "values": _get_model_profile(model_id),
        "load": load_status,
    }


@router.post("/models/load")
async def load_router_model(request: Request):
    """Load a model in llama.cpp router mode."""
    config = _get_config()
    backend_url = _require_backend_url(config, "llama-router")
    payload = _parse_json_object(await request.body(), required=True)

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
            router_models = data.get("data", [])
            if isinstance(router_models, list):
                _attach_model_load_defaults(router_models)
                router_models = _collapse_split_models(router_models)
            for m in router_models:
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
