from __future__ import annotations

import json
import re
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from agent_army.goal_profile import GoalProfile


@dataclass(frozen=True, slots=True)
class WorkspaceSnapshot:
    root: Path
    files: list[str]
    entrypoint: str | None
    manifest_path: Path

    def metadata(self) -> dict[str, Any]:
        return {
            "workspace_path": str(self.root),
            "files": list(self.files),
            "entrypoint": self.entrypoint,
            "manifest_path": str(self.manifest_path),
        }


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self._root = root

    def run_root(self, run_id: str, goal: str, existing_root: Path | None = None) -> Path:
        if existing_root is not None:
            existing_root.mkdir(parents=True, exist_ok=True)
            return existing_root
        return self._ensure_run_root(run_id, goal)

    def task_root(
        self,
        run_id: str,
        goal: str,
        task_id: str,
        title: str,
        sequence_index: int | None = None,
        existing_root: Path | None = None,
    ) -> Path:
        task_name = self._task_name(title, prefix=f"{sequence_index + 1:02d}" if sequence_index is not None else None)
        return self.run_root(run_id, goal, existing_root) / "tasks" / task_name

    def final_root(self, run_id: str, goal: str, existing_root: Path | None = None) -> Path:
        return self.run_root(run_id, goal, existing_root) / "final"

    def materialize_task_output(
        self,
        *,
        run_id: str,
        task_id: str,
        goal: str,
        title: str,
        sequence_index: int | None,
        profile: GoalProfile,
        phase: str,
        raw_output: str,
        existing_root: Path | None = None,
    ) -> WorkspaceSnapshot:
        root = self.task_root(run_id, goal, task_id, title, sequence_index, existing_root)
        file_map = self._build_file_map(profile=profile, phase=phase, raw_output=raw_output)
        entrypoint = "index.html" if "index.html" in file_map else None
        return self._write_workspace(root=root, file_map=file_map, entrypoint=entrypoint, phase=phase)

    def materialize_final_output(
        self,
        *,
        run_id: str,
        goal: str,
        profile: GoalProfile,
        raw_output: str,
        existing_root: Path | None = None,
    ) -> WorkspaceSnapshot:
        root = self.final_root(run_id, goal, existing_root)
        file_map = self._build_file_map(profile=profile, phase="final", raw_output=raw_output)
        entrypoint = "index.html" if "index.html" in file_map else None
        return self._write_workspace(root=root, file_map=file_map, entrypoint=entrypoint, phase="final")

    def _write_workspace(
        self,
        *,
        root: Path,
        file_map: dict[str, str],
        entrypoint: str | None,
        phase: str,
    ) -> WorkspaceSnapshot:
        if root.exists():
            shutil.rmtree(root)
        root.mkdir(parents=True, exist_ok=True)

        written_files: list[str] = []
        for relative_path, content in file_map.items():
            safe_path = self._safe_relative_path(relative_path)
            destination = root / safe_path
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_text(content, encoding="utf-8")
            written_files.append(safe_path.as_posix())

        manifest_path = root / "artifact_manifest.json"
        manifest = {
            "phase": phase,
            "entrypoint": entrypoint,
            "files": [
                {
                    "path": file_path,
                    "size": (root / file_path).stat().st_size,
                }
                for file_path in written_files
            ],
        }
        manifest_path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
        written_files.append("artifact_manifest.json")
        return WorkspaceSnapshot(
            root=root,
            files=written_files,
            entrypoint=entrypoint,
            manifest_path=manifest_path,
        )

    def _build_file_map(self, *, profile: GoalProfile, phase: str, raw_output: str) -> dict[str, str]:
        cleaned = raw_output.strip()
        if phase in {"contract", "verification"}:
            return {"report.md": self._strip_code_fence(cleaned)}
        if profile.artifact_format == "single_html":
            return {"index.html": self._extract_single_html(cleaned)}

        bundle = self._extract_file_bundle(cleaned)
        if bundle:
            return bundle

        fallback_name = "main.py" if profile.language_hint == "python" else "index.js"
        return {fallback_name: self._strip_code_fence(cleaned)}

    @staticmethod
    def _safe_relative_path(value: str) -> Path:
        path = Path(value.strip().replace("\\", "/"))
        if path.is_absolute():
            raise ValueError(f"Absolute paths are not allowed in workspace output: {value}")
        if any(part in {"..", ""} for part in path.parts):
            raise ValueError(f"Unsafe relative path in workspace output: {value}")
        return path

    @staticmethod
    def _strip_code_fence(text: str) -> str:
        fence = re.fullmatch(r"```[^\n]*\n(.*)\n```", text, re.DOTALL)
        if fence:
            return fence.group(1).strip() + "\n"
        return text + ("\n" if not text.endswith("\n") else "")

    def _extract_single_html(self, text: str) -> str:
        html = self._extract_fenced_block(text, preferred_language="html")
        if html is not None:
            return html
        if "<!doctype html" in text.lower() or "<html" in text.lower():
            return self._strip_code_fence(text)
        return self._strip_code_fence(text)

    @staticmethod
    def _extract_fenced_block(text: str, *, preferred_language: str | None = None) -> str | None:
        pattern = re.compile(r"```(?P<lang>[^\n`]*)\n(?P<body>.*?)\n```", re.DOTALL)
        matches = list(pattern.finditer(text))
        if not matches:
            return None
        if preferred_language is not None:
            for match in matches:
                if match.group("lang").strip().lower() == preferred_language.lower():
                    return match.group("body").strip() + "\n"
        return matches[0].group("body").strip() + "\n"

    def _extract_file_bundle(self, text: str) -> dict[str, str]:
        pattern = re.compile(
            r"(?:^|\n)FILE:\s*(?P<path>[^\n]+)\n```(?P<lang>[^\n`]*)\n(?P<body>.*?)\n```",
            re.DOTALL,
        )
        bundle: dict[str, str] = {}
        for match in pattern.finditer(text):
            path = self._safe_relative_path(match.group("path")).as_posix()
            bundle[path] = match.group("body").strip() + "\n"
        return bundle

    @staticmethod
    def _slugify(label: str) -> str:
        slug = re.sub(r"[^a-z0-9]+", "-", label.lower()).strip("-")
        slug = slug or "run"
        return slug[:64].rstrip("-")

    def _ensure_run_root(self, run_id: str, goal: str) -> Path:
        self._root.mkdir(parents=True, exist_ok=True)
        base_name = self._slugify(goal)
        candidate = self._root / base_name
        suffix = 2
        while True:
            marker_path = candidate / ".agent-army-run.json"
            if candidate.exists():
                if marker_path.exists():
                    try:
                        marker = json.loads(marker_path.read_text(encoding="utf-8"))
                    except json.JSONDecodeError:
                        marker = {}
                    if marker.get("run_id") == run_id:
                        return candidate
                candidate = self._root / f"{base_name}-{suffix}"
                suffix += 1
                continue

            candidate.mkdir(parents=True, exist_ok=True)
            marker_path.write_text(json.dumps({"run_id": run_id, "goal": goal}, indent=2), encoding="utf-8")
            return candidate

    def _task_name(self, title: str, prefix: str | None = None) -> str:
        slug = self._slugify(title)
        if prefix:
            return f"{prefix}-{slug}"
        return slug
