from agent_army.services.planner import PlannerService


def test_fallback_plan_obeys_max_steps() -> None:
    planner = PlannerService(client=None, model="unused", temperature=0.0, max_plan_steps=2)  # type: ignore[arg-type]
    result = planner.fallback_plan("Build a market entry strategy")

    assert len(result.subtasks) == 2
    assert result.subtasks[0].depends_on_indexes == []
    assert result.subtasks[1].depends_on_indexes == [0]


def test_code_plan_uses_integration_first_structure() -> None:
    planner = PlannerService(client=None, model="unused", temperature=0.0, max_plan_steps=8)  # type: ignore[arg-type]
    result = planner.fallback_plan("Make a checkers html5 game")

    assert len(result.subtasks) == 4
    assert result.subtasks[1].title == "Build complete runnable artifact"
    assert result.subtasks[1].output_format == "code"
    assert result.subtasks[3].title == "Produce corrected final artifact"
