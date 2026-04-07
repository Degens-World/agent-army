from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class GoalProfile:
    domain: str
    artifact_format: str
    language_hint: str
    final_output_instruction: str


def infer_goal_profile(goal: str) -> GoalProfile:
    text = goal.lower()

    coding_signals = [
        "code",
        "build",
        "implement",
        "game",
        "html",
        "javascript",
        "python",
        "app",
        "api",
        "cli",
        "script",
        "css",
    ]
    is_code = any(signal in text for signal in coding_signals)

    if not is_code:
        return GoalProfile(
            domain="general",
            artifact_format="document",
            language_hint="plain text",
            final_output_instruction="Produce a polished final response in plain text.",
        )

    if "html" in text and "game" in text:
        return GoalProfile(
            domain="coding",
            artifact_format="single_html",
            language_hint="html/css/javascript",
            final_output_instruction=(
                "Produce one complete runnable `index.html` with embedded CSS and JavaScript. "
                "Return code only inside a fenced `html` block. The system will save it as a project folder."
            ),
        )

    if any(signal in text for signal in ["javascript", "typescript", "node"]):
        return GoalProfile(
            domain="coding",
            artifact_format="code_bundle",
            language_hint="javascript",
            final_output_instruction=(
                "Produce complete runnable code as a compact file set. For each file, emit `FILE: relative/path` on its own line "
                "followed by a fenced code block."
            ),
        )

    return GoalProfile(
        domain="coding",
        artifact_format="code_bundle",
        language_hint="python",
        final_output_instruction=(
            "Produce complete runnable code as a compact file set. For each file, emit `FILE: relative/path` on its own line "
            "followed by a fenced code block."
        ),
    )
