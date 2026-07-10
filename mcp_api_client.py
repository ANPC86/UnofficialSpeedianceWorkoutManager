"""Read-only Speediance MCP adapter for exporter-compatible API calls.

This client speaks MCP over Streamable HTTP and intentionally exposes only the
two read methods the custom-workout exporter needs. It never receives or stores
Speediance credentials; authentication is the independent inbound MCP bearer
token supplied by the operator environment. Both the endpoint URL and the
bearer token come from the environment (SPEEDIANCE_MCP_URL and
SPEEDIANCE_MCP_BEARER_TOKEN); no endpoint is hardcoded here.
"""

from __future__ import annotations

import json
import os
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


class SpeedianceMcpError(RuntimeError):
    """MCP adapter failure. Messages must not contain bearer-token values."""


class SpeedianceMcpClient:
    def __init__(self, url: str | None = None, bearer_token: str | None = None, *, timeout: int = 30):
        self.url = (url or os.environ.get("SPEEDIANCE_MCP_URL") or "").strip()
        self.bearer_token = bearer_token or os.environ.get("SPEEDIANCE_MCP_BEARER_TOKEN")
        self.timeout = timeout
        self.session_id: str | None = None
        self.last_debug_info: dict[str, Any] = {}
        # Matches the direct client shape closely enough for the exporter gate.
        self.credentials = {"mcp_bearer_token": "present"} if self.bearer_token else {}

    @classmethod
    def configured_from_env(cls) -> bool:
        return bool(os.environ.get("SPEEDIANCE_MCP_URL") and os.environ.get("SPEEDIANCE_MCP_BEARER_TOKEN"))

    def credentials_available(self) -> bool:
        return bool(self.bearer_token and self.url)

    def _headers(self) -> dict[str, str]:
        if not self.bearer_token:
            raise SpeedianceMcpError("SPEEDIANCE_MCP_BEARER_TOKEN is not available")
        headers = {
            "Accept": "application/json, text/event-stream",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }
        if self.session_id:
            headers["mcp-session-id"] = self.session_id
        return headers

    def _parse_response(self, status_code: int, text: str, headers: Any) -> dict[str, Any] | None:
        if status_code == 202 and not text.strip():
            return None
        if status_code == 401:
            raise SpeedianceMcpError("MCP authorization failed")
        if status_code >= 400:
            raise SpeedianceMcpError(f"MCP request failed: HTTP {status_code}")

        text = text.strip()
        if not text:
            return None
        if text.startswith("event:") or "\ndata:" in text or text.startswith("data:"):
            data_lines = []
            for line in text.splitlines():
                if line.startswith("data:"):
                    data_lines.append(line[5:].strip())
            text = "\n".join(data_lines).strip()
        try:
            payload = json.loads(text)
        except ValueError as exc:
            raise SpeedianceMcpError("MCP returned a non-JSON response") from exc
        if not isinstance(payload, dict):
            raise SpeedianceMcpError("MCP returned an unexpected response shape")
        return payload

    def _post(self, payload: dict[str, Any]) -> dict[str, Any] | None:
        if not self.url:
            raise SpeedianceMcpError("SPEEDIANCE_MCP_URL is not available")
        data = json.dumps(payload).encode("utf-8")
        request = Request(self.url, data=data, headers=self._headers(), method="POST")
        try:
            with urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
                status_code = response.status
                headers = response.headers
        except HTTPError as exc:
            raw = exc.read().decode("utf-8", errors="replace")
            status_code = exc.code
            headers = exc.headers
        except URLError as exc:
            raise SpeedianceMcpError(f"MCP connection failed: {exc.reason}") from exc

        if headers.get("mcp-session-id"):
            self.session_id = headers["mcp-session-id"]
        self.last_debug_info = {
            "method": payload.get("method"),
            "status": status_code,
            "has_session": bool(self.session_id),
        }
        return self._parse_response(status_code, raw, headers)

    def _ensure_initialized(self) -> None:
        if self.session_id:
            return
        payload = {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-06-18",
                "capabilities": {},
                "clientInfo": {"name": "SmartGymWorkoutManager", "version": "custom-workout-export"},
            },
        }
        result = self._post(payload)
        if not result or "error" in result:
            raise SpeedianceMcpError("MCP initialize failed")
        self._post({"jsonrpc": "2.0", "method": "notifications/initialized"})

    @staticmethod
    def _decode_tool_result(payload: dict[str, Any] | None) -> dict[str, Any]:
        if not payload:
            raise SpeedianceMcpError("MCP tool returned no response")
        if payload.get("error"):
            raise SpeedianceMcpError("MCP tool returned an error")
        result = payload.get("result")
        if not isinstance(result, dict):
            raise SpeedianceMcpError("MCP tool returned an unexpected result")
        if result.get("isError"):
            raise SpeedianceMcpError("MCP tool reported an error")
        structured = result.get("structuredContent")
        if isinstance(structured, dict):
            return structured
        for item in result.get("content") or []:
            if isinstance(item, dict) and item.get("type") == "text":
                try:
                    decoded = json.loads(item.get("text") or "")
                except ValueError as exc:
                    raise SpeedianceMcpError("MCP tool text was not JSON") from exc
                if isinstance(decoded, dict):
                    return decoded
        raise SpeedianceMcpError("MCP tool response did not contain structured data")

    def call_tool(self, name: str, arguments: dict[str, Any] | None = None) -> dict[str, Any]:
        self._ensure_initialized()
        payload = {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": name, "arguments": arguments or {}},
        }
        return self._decode_tool_result(self._post(payload))

    def get_user_workouts(self) -> list[dict[str, Any]]:
        result = self.call_tool("speediance_list_custom_templates")
        if result.get("status") not in (None, "ok"):
            raise SpeedianceMcpError("MCP custom-template list failed")
        templates = result.get("templates")
        return templates if isinstance(templates, list) else []

    def get_workout_detail(self, code: str) -> dict[str, Any] | None:
        result = self.call_tool(
            "speediance_get_custom_template_detail",
            {"code": code, "include_detail": True},
        )
        if result.get("status") not in (None, "ok"):
            raise SpeedianceMcpError("MCP custom-template detail failed")
        detail = result.get("detail")
        return detail if isinstance(detail, dict) else None
