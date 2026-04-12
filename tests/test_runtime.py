from pathlib import Path

import pytest

from agent_army.config import Settings
from agent_army.runtime import AgentArmyRuntime


@pytest.mark.asyncio
async def test_reopen_run_carries_source_workspace_metadata(tmp_path: Path) -> None:
    settings = Settings(
        db_path=tmp_path / "agent_army.db",
        workspace_root=tmp_path / "runs",
    )
    runtime = AgentArmyRuntime.from_settings(settings)
    await runtime.db.initialize()

    source_run_id = await runtime.db.create_run(goal="Make an html5 checkers game", metadata={})
    artifact_id = await runtime.db.create_artifact(
        run_id=source_run_id,
        task_id=None,
        kind="final",
        content="```html\n<html></html>\n```",
        metadata={
            "workspace_path": str(tmp_path / "runs" / source_run_id / "final"),
            "entrypoint": "index.html",
            "files": ["index.html", "artifact_manifest.json"],
            "manifest_path": str(tmp_path / "runs" / source_run_id / "final" / "artifact_manifest.json"),
        },
    )
    await runtime.db.set_final_artifact(source_run_id, artifact_id)

    reopened_run_id = await runtime.reopen_run(
        source_run_id=source_run_id,
        instructions="Fix the restart button",
    )

    reopened = await runtime.db.get_run(reopened_run_id)
    assert reopened is not None
    reopen = reopened.metadata.get("reopen")
    assert isinstance(reopen, dict)
    assert reopen["source_run_id"] == source_run_id
    assert reopen["instructions"] == "Fix the restart button"
    assert reopen["source_workspace_path"].endswith(f"{source_run_id}\\final")
    assert reopen["source_project_root"].endswith(source_run_id)
