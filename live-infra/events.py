from typing import Callable
from dataclasses import dataclass


@dataclass(frozen=True)
class Event:
    """An observable core change: state, delta, response, result, or error."""

    type: str
    value: object = None


class Emitter:
    def __init__(self, observer: Callable[[Event], None] | None = None) -> None:
        self.observer = observer or (lambda _event: None)

    def _emit(self, event: Event) -> None:
        """Publish a core event to the optional presentation or embedding."""
        self.observer(event)


def render(event: Event) -> None:
    if event.type == "generation":
        print(f"[generation]\n{event.value}")
    elif event.type == "parse":
        print(f"[parse] {event.value}")
    elif event.type == "action":
        print(f"[action] {event.value}")
    elif event.type == "tool_call":
        print(f"[tool_call] {event.value}")
    elif event.type == "tool_return":
        print(f"[tool_return] {event.value}")
    elif event.type == "observation_rendered":
        print(f"[observation] {event.value}")
    elif event.type == "response":
        print(f"[response] {event.value}")
    elif event.type == "error":
        print(f"[error] {event.value}")
