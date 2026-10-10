"""Ejecuta una subtarea con arc-cua y devuelve un resultado para el planificador.

El planificador (Claude) entrega una subtarea acotada; aquí se aplican los
límites, se traduce el final a un estado estable y se quitan los secretos.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Mapping

from arc_cua import DesktopExecutor, RuntimeConfig, result_to_dict, subtask_from_dict
from arc_cua.errors import JevDesktopError
from arc_cua.safety import RISK_CATEGORIES, redact

from .jev import QuotaExceeded, Usage

log = logging.getLogger(__name__)

MAX_ACTIONS_CAP = 40

# La barrera de riesgos de arc-cua solo reconoce inglés y solo clics. Se le
# permiten todas sus categorías para que no interfiera: la barrera propia
# (gate.py) envuelve al backend y ve todas las acciones.
_ARC_RISKS = tuple(sorted(RISK_CATEGORIES))


@dataclass(frozen=True)
class Limits:
    max_actions: int = 30
    operation_seconds: float = 300.0


def run_subtask(
    backend: Any,
    policy: Any,
    payload: Mapping[str, Any],
    *,
    limits: Limits = Limits(),
    on_step: Callable[[dict], None] | None = None,
    cancel: threading.Event | None = None,
    usage: Usage | None = None,
    price_per_mtok: float = 0.0,
    paused: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Devuelve `{status, reason, needs_input, actions_taken, page, metrics}`.

    `status`: SUBTASK_COMPLETE, NEEDS_INPUT, NEEDS_AGENT, BLOCKED, LIMIT, QUOTA o CANCELLED.

    `paused()` devuelve los segundos pasados esperando a Pablo (una aprobación): no
    cuentan para el tiempo máximo de la operación.
    """
    payload = dict(payload)
    requested = payload.get("max_actions", limits.max_actions)
    payload["max_actions"] = max(1, min(int(requested), MAX_ACTIONS_CAP))
    payload["allowed_risks"] = _ARC_RISKS
    subtask = subtask_from_dict(payload)
    secrets = subtask.secret_values

    # El tiempo máximo se controla aquí y no en arc-cua, que no sabe descontar las esperas.
    executor = DesktopExecutor(backend, policy, config=RuntimeConfig(timeout_s=None))
    timed_out = False
    usage_before = usage.copy() if usage is not None else None
    started = time.monotonic()
    step_ms: list[int] = []
    result = None
    failure: tuple[str, str] | None = None

    try:
        for event in executor.run_iter(subtask):
            if event.record is not None:
                step_ms.append(event.record.elapsed_ms)
                if on_step is not None:
                    on_step(redact({
                        "n": event.record.step,
                        "action": event.record.action.kind.value,
                        "target": event.record.target_name,
                        "ms": event.decision.latency_ms,
                    }, secrets))
            if event.terminal:
                result = event.result
            elif cancel is not None and cancel.is_set():
                executor.cancel()
            elif time.monotonic() - started - (paused() if paused else 0.0) > limits.operation_seconds:
                timed_out = True
                executor.cancel()
    except QuotaExceeded as exc:
        failure = ("QUOTA", str(exc))
    except (JevDesktopError, RuntimeError, ValueError) as exc:
        # Decisión inválida, JEV caído o destino desaparecido: nada se ejecutó en este paso.
        failure = ("NEEDS_AGENT", redact(str(exc), secrets))

    if result is not None:
        data = result_to_dict(result)
        status, reason = _status(data["status"], data.get("reason"))
        if timed_out and status == "CANCELLED":
            status, reason = "LIMIT", "Se alcanzó el tiempo máximo de la operación."
        out = {
            "status": status,
            "reason": reason,
            "needs_input": data.get("needs_input"),
            "actions_taken": data["actions_taken"],
            "page": data["final_snapshot"],
        }
    else:
        status, reason = failure or ("NEEDS_AGENT", "La ejecución terminó sin resultado.")
        out = {
            "status": status,
            "reason": reason,
            "needs_input": None,
            "actions_taken": len(step_ms),
            "page": _safe_page(backend, secrets),
        }

    spent = usage.since(usage_before) if usage is not None and usage_before is not None else Usage()
    out["metrics"] = {
        "steps": len(step_ms),
        "seconds": round(time.monotonic() - started, 2),
        "step_ms": step_ms,
        "jev_calls": spent.calls,
        "jev_ms": spent.ms,
        "input_tokens": spent.input_tokens,
        "cost_usd": round(spent.cost_usd(price_per_mtok), 6),
    }
    return out


def _status(arc_status: str, reason: str | None) -> tuple[str, str | None]:
    if arc_status != "NEEDS_AGENT" or not reason:
        return arc_status, reason
    if reason.startswith("Execution cancelled"):
        return "CANCELLED", "Cancelado."
    if reason.startswith("Reached agent-supplied action budget"):
        return "LIMIT", "Se alcanzó el número máximo de pasos."
    return arc_status, reason


def _safe_page(backend: Any, secrets: tuple[str, ...]) -> dict | None:
    try:
        return redact(backend.observe().compact(), secrets)
    except Exception:
        log.debug("no se pudo observar la página tras el fallo", exc_info=True)
        return None
