"""Ejecución de subtareas con límites, sobre una aplicación en memoria (sin Chromium)."""

from __future__ import annotations

import json
import threading
import time
import unittest
from typing import Any

from arc_cua import ActionKind, Decision, DesktopElement, DesktopSnapshot, TerminalKind
from arc_cua.backends.memory import StateMachineBackend

from lisa_browser.runner import Limits, run_subtask

SUBTASK = {"goal": "Pulsa Sumar", "verification": ["El contador ha subido"]}
SECRET = "s3creta-muy-larga"


def counter_app() -> tuple[StateMachineBackend, dict[str, Any]]:
    """Un contador con un botón y un campo de texto. Devuelve el backend y su estado vivo."""

    def snapshot(state: dict[str, Any]) -> DesktopSnapshot:
        elements = (
            DesktopElement(id="sumar", role="button", name="Sumar", actions=(ActionKind.CLICK,)),
            DesktopElement(id="nombre", role="textbox", name="Nombre", value=state["text"],
                           actions=(ActionKind.TYPE_TEXT,)),
            DesktopElement(id="total", role="text", name=f"Contador: {state['count']}"),
        )
        return DesktopSnapshot(
            application="Prueba", window="Contador", revision=f"{state['count']}|{state['text']}", elements=elements,
        )

    def transition(state: dict[str, Any], action: Any) -> None:
        if action.kind == ActionKind.CLICK:
            state["count"] += 1
        elif action.kind == ActionKind.TYPE_TEXT:
            state["text"] = str(action.value)

    backend = StateMachineBackend({"count": 0, "text": ""}, snapshot, transition)
    return backend, backend.state


class Steps:
    """Política fijada: una lista de decisiones; con `forever`, la última se repite."""

    def __init__(self, *decisions: Decision, forever: bool = False, delay: float = 0.0) -> None:
        self._decisions = list(decisions)
        self._forever = forever
        self._delay = delay

    def decide(self, **_: Any) -> Decision:
        time.sleep(self._delay)
        if self._forever and len(self._decisions) == 1:
            return self._decisions[0]
        return self._decisions.pop(0)


CLICK = Decision(kind=ActionKind.CLICK, target_id="sumar")
DONE = Decision(terminal=TerminalKind.SUBTASK_COMPLETE)


class RunSubtask(unittest.TestCase):
    def test_complete_returns_page_and_metrics(self) -> None:
        backend, state = counter_app()
        result = run_subtask(backend, Steps(CLICK, DONE), SUBTASK)
        self.assertEqual((result["status"], result["actions_taken"], state["count"]), ("SUBTASK_COMPLETE", 1, 1))
        self.assertIn("Contador: 1", json.dumps(result["page"]))
        self.assertEqual(result["metrics"]["steps"], 1)

    def test_action_budget_ends_as_limit(self) -> None:
        backend, state = counter_app()
        result = run_subtask(backend, Steps(CLICK, forever=True), {**SUBTASK, "max_actions": 3})
        self.assertEqual((result["status"], state["count"]), ("LIMIT", 3))

    def test_requested_budget_is_capped(self) -> None:
        backend, state = counter_app()
        run_subtask(backend, Steps(CLICK, forever=True), {**SUBTASK, "max_actions": 500})
        self.assertEqual(state["count"], 40)

    def test_time_limit_ends_as_limit(self) -> None:
        backend, state = counter_app()
        result = run_subtask(
            backend, Steps(CLICK, forever=True, delay=0.05), SUBTASK, limits=Limits(operation_seconds=0.2),
        )
        self.assertEqual(result["status"], "LIMIT")
        self.assertLess(state["count"], 30)

    def test_cancel_stops_at_the_next_step(self) -> None:
        backend, state = counter_app()
        cancel = threading.Event()
        result = run_subtask(
            backend, Steps(CLICK, forever=True), SUBTASK, cancel=cancel, on_step=lambda step: cancel.set(),
        )
        self.assertEqual(result["status"], "CANCELLED")
        self.assertLessEqual(state["count"], 2)

    def test_blocked_and_needs_input_are_passed_through(self) -> None:
        for terminal in (TerminalKind.BLOCKED, TerminalKind.NEEDS_INPUT, TerminalKind.NEEDS_AGENT):
            with self.subTest(terminal=terminal.value):
                backend, _ = counter_app()
                result = run_subtask(backend, Steps(Decision(terminal=terminal)), SUBTASK)
                self.assertEqual(result["status"], terminal.value)

    def test_invalid_decision_is_reported_without_acting(self) -> None:
        backend, state = counter_app()
        result = run_subtask(backend, Steps(Decision(kind=ActionKind.CLICK, target_id="no-existe")), SUBTASK)
        self.assertEqual((result["status"], state["count"]), ("NEEDS_AGENT", 0))
        self.assertIsNotNone(result["page"])

    def test_secret_input_is_typed_but_never_returned(self) -> None:
        backend, state = counter_app()
        seen: list[dict] = []
        result = run_subtask(
            backend,
            Steps(Decision(kind=ActionKind.TYPE_TEXT, target_id="nombre", input_key="clave"), DONE),
            {**SUBTASK, "inputs": {"clave": SECRET}, "secret_inputs": ["clave"]},
            on_step=seen.append,
        )
        self.assertEqual(state["text"], SECRET)  # se escribió de verdad…
        self.assertNotIn(SECRET, json.dumps(result))  # …pero no vuelve al planificador
        self.assertNotIn(SECRET, json.dumps(seen))
        self.assertIn("[secret]", json.dumps(result["page"]))


if __name__ == "__main__":
    unittest.main()
