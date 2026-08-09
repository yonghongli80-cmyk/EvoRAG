from rag.memory import DialogueMemory


def test_summary_includes_all_messages_before_first_retained_user(tmp_path):
    captured = []
    memory = DialogueMemory(str(tmp_path / "memory.db"), generate_fn=lambda messages: captured.append(messages) or "summary")
    for round_no in range(11):
        memory.add_message("s", "user", f"user-{round_no}")
        memory.add_message("s", "assistant", f"assistant-{round_no}-a")
        memory.add_message("s", "assistant", f"assistant-{round_no}-b")

    memory.summarize_old_rounds("s")

    summarized = captured[0][1]["content"]
    assert "user-0" in summarized
    assert "assistant-0-a" in summarized
    assert "assistant-0-b" in summarized
    assert "user-1" not in summarized
    assert memory._conn.execute(
        "SELECT last_summarized_count FROM sessions WHERE session_id = 's'"
    ).fetchone()[0] == 11
