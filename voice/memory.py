"""In-session, RAM-only rolling conversation buffer.

Deliberately not persistent — full cross-restart memory (PostgreSQL/pgvector) is the separately
queued memory subplan (see TASK_QUEUE.md), not this voice loop.
"""

from collections import deque
from typing import Literal, TypedDict


class Turn(TypedDict):
    role: str
    content: str


class ConversationBuffer:
    def __init__(self, max_turns: int = 20) -> None:
        self._turns: deque[Turn] = deque(maxlen=max_turns)

    def add(self, role: Literal["user", "assistant"], text: str) -> None:
        self._turns.append({"role": role, "content": text})

    def to_history(self) -> list[Turn]:
        return list(self._turns)
