"""Ayudas de las pruebas: Chromium sin interfaz y una política guionizada."""

from __future__ import annotations

import os
import shlex
from typing import Any, Sequence

from arc_cua import ActionKind, Decision, TerminalKind

from lisa_browser.session import Browser

# Por si el entorno de pruebas no permite el aislamiento de procesos de Chromium.
_EXTRA_ARGS = tuple(shlex.split(os.environ.get("LISA_TEST_CHROME_ARGS", "")))


def launch_browser() -> Browser:
    return Browser.launch(headless=True, extra_args=_EXTRA_ARGS)


class ByName:
    """Política de pruebas: sustituye a JEV con una lista de pasos fijada.

    Cada paso es `("CLICK", "Nombre del elemento")`, `("TYPE_TEXT", "Campo", "clave_de_input")`,
    `("TYPE_TEXT", "Campo", "clave", "ENTER")` o un final: `("SUBTASK_COMPLETE",)`,
    `("NEEDS_INPUT", "Campo")`, `("NEEDS_AGENT",)`, `("BLOCKED",)`.
    Con `repeat=True`, el último paso se repite sin fin.
    """

    def __init__(self, steps: Sequence[tuple], *, repeat: bool = False) -> None:
        self._steps = list(steps)
        self._repeat = repeat
        self.calls = 0

    def decide(self, *, subtask: Any, snapshot: Any, history: Any) -> Decision:
        self.calls += 1
        if not self._steps:
            raise RuntimeError("la política de pruebas se quedó sin pasos")
        step = self._steps[0] if self._repeat and len(self._steps) == 1 else self._steps.pop(0)
        name, *rest = step
        if name in TerminalKind.__members__:
            target = self._find(snapshot, rest[0]) if rest else None
            return Decision(terminal=TerminalKind[name], target_id=target)
        kind = ActionKind[name]
        target = self._find(snapshot, rest[0], kind)
        input_key = rest[1] if len(rest) > 1 else None
        key = rest[2] if len(rest) > 2 else None
        return Decision(kind=kind, target_id=target, input_key=input_key, key=key)

    @staticmethod
    def _find(snapshot: Any, name: str, kind: ActionKind | None = None) -> str:
        for element in snapshot.elements:
            if element.name == name and (kind is None or kind in element.actions):
                return element.id
        names = sorted({e.name for e in snapshot.elements if e.name})
        raise AssertionError(f"no hay elemento {name!r} en la página; hay: {names}")
