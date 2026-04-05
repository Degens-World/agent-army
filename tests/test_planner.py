from agent_army.services.planner import PlannerService


def test_fallback_plan_obeys_max_steps() -> None:
    planner = PlannerService(client=None, model="unused", temperature=0.0, max_plan_steps=2)  # type: ignore[arg-type]
    result = planner.fallback_plan("Build a market entry strategy")

    assert len(result.subtasks) == 2
    assert result.subtasks[0].depends_on_indexes == []
    assert result.subtasks[1].depends_on_indexes == [0]
