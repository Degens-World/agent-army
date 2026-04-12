from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


CHAT_HELP = """Commands:
  <text>         Start a new run from the text you enter.
  /runs          List recent runs.
  /watch <id>    Watch an existing run.
  /revise <id>
                 Reopen an existing run and prompt for what to modify or fix.
  /revise <id> <instructions>
                 Reopen an existing run with inline revision instructions.
  /help          Show this help.
  /quit          Exit chat.
"""


@dataclass(slots=True)
class ChatCommand:
    kind: str
    value: str = ""


def classify_chat_input(text: str) -> ChatCommand:
    stripped = text.strip()
    lowered = stripped.lower()
    if not stripped:
        return ChatCommand(kind="empty")
    if lowered in {"/quit", "/exit"}:
        return ChatCommand(kind="quit")
    if lowered == "/help":
        return ChatCommand(kind="help")
    if lowered == "/runs":
        return ChatCommand(kind="runs")
    if lowered.startswith("/watch "):
        return ChatCommand(kind="watch", value=stripped.split(" ", 1)[1].strip())
    if lowered in {"/revise", "/reopen"}:
        return ChatCommand(kind="revise_prompt")
    if lowered.startswith("/revise ") or lowered.startswith("/reopen "):
        parts = stripped.split(" ", 2)
        if len(parts) == 2:
            return ChatCommand(kind="revise_prompt", value=parts[1].strip())
        if len(parts) >= 3:
            return ChatCommand(kind="revise", value=f"{parts[1].strip()}|{parts[2].strip()}")
    return ChatCommand(kind="task", value=stripped)


def chat_result_path(run_id: str) -> Path:
    return Path("output") / f"chat-{run_id}.md"
