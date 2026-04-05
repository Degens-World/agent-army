from __future__ import annotations

from agent_army.prompts import SYNTHESIZER_SYSTEM_PROMPT, SYNTHESIZER_USER_TEMPLATE
from agent_army.services.ollama import OllamaClient


class SynthesizerService:
    def __init__(self, client: OllamaClient, model: str, temperature: float) -> None:
        self._client = client
        self._model = model
        self._temperature = temperature

    async def synthesize(self, goal: str, artifacts: list[str]) -> str:
        joined = "\n\n".join(f"Subtask {index + 1}:\n{artifact}" for index, artifact in enumerate(artifacts))
        prompt = SYNTHESIZER_USER_TEMPLATE.format(goal=goal, artifacts=joined)
        return await self._client.generate(
            model=self._model,
            system=SYNTHESIZER_SYSTEM_PROMPT,
            prompt=prompt,
            temperature=self._temperature,
        )
