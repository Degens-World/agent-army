from __future__ import annotations

import json
import re
from typing import Any

import httpx


class OllamaError(RuntimeError):
    """Raised when Ollama returns an API-level error."""


class OllamaClient:
    def __init__(self, host: str, timeout_seconds: float) -> None:
        self._host = host.rstrip("/")
        self._timeout = timeout_seconds

    async def generate(
        self,
        *,
        model: str,
        system: str,
        prompt: str,
        temperature: float,
    ) -> str:
        payload = {
            "model": model,
            "system": system,
            "prompt": prompt,
            "stream": False,
            "options": {"temperature": temperature},
        }
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.post(f"{self._host}/api/generate", json=payload)
            if response.is_error:
                raise OllamaError(self._format_error(response, model))
        data = response.json()
        return data.get("response", "").strip()

    async def list_models(self) -> list[str]:
        async with httpx.AsyncClient(timeout=self._timeout) as client:
            response = await client.get(f"{self._host}/api/tags")
            if response.is_error:
                raise OllamaError(self._format_error(response, "model listing"))
        data = response.json()
        return [item["name"] for item in data.get("models", []) if "name" in item]

    @staticmethod
    def _format_error(response: httpx.Response, model: str) -> str:
        try:
            payload = response.json()
            detail = payload.get("error") or payload
        except ValueError:
            detail = response.text
        return f"Ollama request failed for {model}: HTTP {response.status_code} - {detail}"

    @staticmethod
    def extract_json(text: str) -> dict[str, Any]:
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass

        fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.DOTALL)
        if fenced:
            return json.loads(fenced.group(1))

        obj = re.search(r"(\{.*\})", text, re.DOTALL)
        if obj:
            return json.loads(obj.group(1))

        raise ValueError("Model response did not contain valid JSON.")
