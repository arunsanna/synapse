"""HTTP helpers for gateway route handlers."""

from fastapi.responses import JSONResponse


def json_or_error_response(resp, error_label: str) -> JSONResponse:
    """Return backend JSON response or a stable gateway error envelope."""
    try:
        payload = resp.json()
    except ValueError:
        payload = {
            "error": error_label,
            "detail": resp.text[:1000],
        }
    return JSONResponse(status_code=resp.status_code, content=payload)
