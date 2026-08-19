from voice.memory import ConversationBuffer


def test_empty_buffer_returns_empty_history():
    buffer = ConversationBuffer()
    assert buffer.to_history() == []


def test_add_preserves_order():
    buffer = ConversationBuffer()
    buffer.add("user", "set the volume to forty")
    buffer.add("assistant", "done, volume is at forty")

    assert buffer.to_history() == [
        {"role": "user", "content": "set the volume to forty"},
        {"role": "assistant", "content": "done, volume is at forty"},
    ]


def test_buffer_evicts_oldest_turns_past_max_turns():
    buffer = ConversationBuffer(max_turns=3)
    for i in range(5):
        buffer.add("user", f"turn {i}")

    history = buffer.to_history()
    assert len(history) == 3
    assert [turn["content"] for turn in history] == ["turn 2", "turn 3", "turn 4"]
