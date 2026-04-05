from __future__ import annotations

import json
import re
from typing import Any

import httpx


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
            response.raise_for_status()
        data = response.json()
        return data.get("response", "").strip()

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
