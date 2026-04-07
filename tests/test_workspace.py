from pathlib import Path

from agent_army.goal_profile import infer_goal_profile
from agent_army.validator import CodingValidator
from agent_army.workspace import WorkspaceManager


def test_single_html_output_materializes_index_html(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    profile = infer_goal_profile("Make an HTML5 checkers game.")

    snapshot = manager.materialize_task_output(
        run_id="run-1",
        task_id="task-1",
        profile=profile,
        phase="implementation",
        raw_output="```html\n<!DOCTYPE html><html><body>checkers<script>const king = true; const capture = true;</script></body></html>\n```",
    )

    assert (snapshot.root / "index.html").exists()
    assert (snapshot.root / "artifact_manifest.json").exists()
    assert snapshot.entrypoint == "index.html"


def test_contract_validator_rejects_html_instead_of_spec(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    profile = infer_goal_profile("Make an HTML5 checkers game.")
    snapshot = manager.materialize_task_output(
        run_id="run-1",
        task_id="task-2",
        profile=profile,
        phase="contract",
        raw_output="```html\n<!DOCTYPE html><html><body>not a contract</body></html>\n```",
    )

    result = CodingValidator().validate(
        goal="Make an HTML5 checkers game.",
        profile=profile,
        phase="contract",
        workspace_path=snapshot.root,
    )

    assert result.approved is False
    assert any("implementation code" in issue for issue in result.issues)
