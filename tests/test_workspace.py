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
        goal="Make an HTML5 checkers game.",
        title="Build complete runnable artifact",
        sequence_index=1,
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
        goal="Make an HTML5 checkers game.",
        title="Define implementation contract",
        sequence_index=0,
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


def test_workspace_paths_use_human_readable_slugs(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    profile = infer_goal_profile("Make an HTML5 checkers game.")

    snapshot = manager.materialize_final_output(
        run_id="16425ac2-3af1-4bd9-9977-bb9a009a6947",
        goal="Make an HTML5 checkers game.",
        profile=profile,
        raw_output="```html\n<!DOCTYPE html><html><body>ok</body></html>\n```",
    )

    root_text = snapshot.root.as_posix()
    assert "make-an-html5-checkers-game/final" in root_text
    assert root_text.endswith("/final")


def test_workspace_paths_use_numeric_suffix_on_collision(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    profile = infer_goal_profile("Make an HTML5 checkers game.")

    first = manager.materialize_final_output(
        run_id="run-1",
        goal="Make an HTML5 checkers game.",
        profile=profile,
        raw_output="```html\n<!DOCTYPE html><html><body>one</body></html>\n```",
    )
    second = manager.materialize_final_output(
        run_id="run-2",
        goal="Make an HTML5 checkers game.",
        profile=profile,
        raw_output="```html\n<!DOCTYPE html><html><body>two</body></html>\n```",
    )

    assert first.root.as_posix().endswith("/make-an-html5-checkers-game/final")
    assert second.root.as_posix().endswith("/make-an-html5-checkers-game-2/final")


def test_existing_root_is_reused_for_revision_materialization(tmp_path: Path) -> None:
    manager = WorkspaceManager(tmp_path)
    profile = infer_goal_profile("Make an HTML5 checkers game.")
    existing_root = tmp_path / "existing-project"

    snapshot = manager.materialize_final_output(
        run_id="new-run",
        goal="Make an HTML5 checkers game.",
        profile=profile,
        raw_output="```html\n<!DOCTYPE html><html><body>ok</body></html>\n```",
        existing_root=existing_root,
    )

    assert snapshot.root == existing_root / "final"
