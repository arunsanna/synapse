"""Kubernetes-backed llama-router runtime arg controller."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import httpx

SERVICE_ACCOUNT_TOKEN_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/token")
SERVICE_ACCOUNT_CA_PATH = Path("/var/run/secrets/kubernetes.io/serviceaccount/ca.crt")

RUNTIME_PROFILE_TO_ROUTER_ARG: dict[str, str] = {
    "runtime_ctx_size": "--ctx-size",
}


def parse_runtime_profile_args(args: list[str]) -> dict[str, int]:
    """Extract runtime profile values from a llama.cpp argv-style list."""
    parsed: dict[str, int] = {}
    for profile_key, flag in RUNTIME_PROFILE_TO_ROUTER_ARG.items():
        if flag not in args:
            continue
        idx = args.index(flag)
        if idx + 1 >= len(args):
            continue
        raw = args[idx + 1]
        try:
            parsed[profile_key] = int(raw)
        except (TypeError, ValueError):
            continue
    return parsed


def apply_runtime_profile_args(args: list[str], runtime_values: dict[str, int]) -> list[str]:
    """Return updated argv with runtime profile values applied."""
    updated = list(args)
    for profile_key, flag in RUNTIME_PROFILE_TO_ROUTER_ARG.items():
        if profile_key not in runtime_values:
            continue
        value = str(int(runtime_values[profile_key]))
        if flag in updated:
            idx = updated.index(flag)
            if idx + 1 < len(updated):
                updated[idx + 1] = value
            else:
                updated.append(value)
        else:
            updated.extend([flag, value])
    return updated


class RouterRuntimeController:
    """Applies llama-router runtime args by patching Kubernetes deployment spec."""

    def __init__(self, namespace: str, deployment_name: str, container_name: str):
        self._namespace = namespace
        self._deployment_name = deployment_name
        self._container_name = container_name

    @property
    def _deployment_path(self) -> str:
        return (
            f"/apis/apps/v1/namespaces/{self._namespace}/deployments/{self._deployment_name}"
        )

    @staticmethod
    def _api_base_url() -> str:
        host = os.getenv("KUBERNETES_SERVICE_HOST")
        port = os.getenv("KUBERNETES_SERVICE_PORT_HTTPS", "443")
        if host:
            return f"https://{host}:{port}"
        return "https://kubernetes.default.svc"

    @staticmethod
    def _read_service_account_token() -> str:
        if not SERVICE_ACCOUNT_TOKEN_PATH.exists():
            raise RuntimeError(
                "Kubernetes service account token not available; runtime reconfigure is only supported in-cluster."
            )
        return SERVICE_ACCOUNT_TOKEN_PATH.read_text(encoding="utf-8").strip()

    @staticmethod
    def _verify_target() -> str | bool:
        if SERVICE_ACCOUNT_CA_PATH.exists():
            return str(SERVICE_ACCOUNT_CA_PATH)
        return True

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        content_type: str | None = None,
    ) -> dict[str, Any]:
        token = self._read_service_account_token()
        headers = {"Authorization": f"Bearer {token}"}
        if content_type:
            headers["Content-Type"] = content_type
        url = f"{self._api_base_url()}{path}"
        async with httpx.AsyncClient(timeout=30.0, verify=self._verify_target()) as session:
            response = await session.request(method, url, headers=headers, json=json_body)
        if response.status_code >= 400:
            detail = response.text.strip() or f"status={response.status_code}"
            raise RuntimeError(
                f"Kubernetes API {method} {path} failed ({response.status_code}): {detail}"
            )
        try:
            payload = response.json()
        except ValueError as e:
            raise RuntimeError(f"Kubernetes API returned invalid JSON for {method} {path}") from e
        if not isinstance(payload, dict):
            raise RuntimeError(f"Kubernetes API returned non-object JSON for {method} {path}")
        return payload

    @staticmethod
    def _extract_container_args(payload: dict[str, Any], container_name: str) -> list[str]:
        containers = (
            payload.get("spec", {})
            .get("template", {})
            .get("spec", {})
            .get("containers", [])
        )
        if not isinstance(containers, list):
            raise RuntimeError("Deployment spec has invalid containers field")
        for container in containers:
            if not isinstance(container, dict):
                continue
            if container.get("name") != container_name:
                continue
            args = container.get("args", [])
            if not isinstance(args, list):
                raise RuntimeError(f"Container '{container_name}' args is not a list")
            return [str(item) for item in args]
        raise RuntimeError(f"Container '{container_name}' not found in deployment")

    async def fetch_container_args(self) -> list[str]:
        deployment = await self._request("GET", self._deployment_path)
        return self._extract_container_args(deployment, self._container_name)

    async def apply_runtime_values(self, runtime_values: dict[str, int]) -> bool:
        """Patch deployment args when runtime values differ. Returns True when patched."""
        current_args = await self.fetch_container_args()
        updated_args = apply_runtime_profile_args(current_args, runtime_values)
        if updated_args == current_args:
            return False
        patch_body = {
            "spec": {
                "template": {
                    "spec": {
                        "containers": [
                            {
                                "name": self._container_name,
                                "args": updated_args,
                            }
                        ]
                    }
                }
            }
        }
        await self._request(
            "PATCH",
            self._deployment_path,
            json_body=patch_body,
            content_type="application/strategic-merge-patch+json",
        )
        return True
