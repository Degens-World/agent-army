from agent_army.chat import chat_result_path, classify_chat_input


def test_classify_chat_input_detects_watch_command() -> None:
    command = classify_chat_input("/watch abc123")

    assert command.kind == "watch"
    assert command.value == "abc123"


def test_classify_chat_input_treats_plain_text_as_task() -> None:
    command = classify_chat_input("Build a plan for an agent system")

    assert command.kind == "task"
    assert "agent system" in command.value


def test_classify_chat_input_is_case_insensitive_for_commands() -> None:
    command = classify_chat_input("/RUNS")

    assert command.kind == "runs"


def test_chat_result_path_uses_output_directory() -> None:
    path = chat_result_path("run-42")

    assert path.as_posix() == "output/chat-run-42.md"
