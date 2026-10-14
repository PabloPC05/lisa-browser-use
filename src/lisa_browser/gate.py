"""Barrera de aprobaciones: ninguna acción con efecto se ejecuta sin permiso (PLAN §5).

Envuelve al backend de Chromium, de modo que toda acción pasa por aquí antes
de ejecutarse, también escribir y pulsar Intro. La decisión de qué acción
«envía», «borra» o «paga» se toma con reglas sobre el texto del control, el
formulario que lo contiene y el sitio. Es una heurística: un control sin texto
reconocible y fuera de un formulario puede no detectarse (límite aceptado).
"""

from __future__ import annotations

import hashlib
import json
import re
import time
import unicodedata
from dataclasses import dataclass
from typing import Any, Callable, Mapping, Sequence
from urllib.parse import urlparse

from arc_cua import ActionKind, Decision, TerminalKind
from arc_cua.errors import StaleDesktopState

from .runner import run_subtask
from .session import Browser

# Verbos, no sustantivos: «Ver pagos» o «Historial de compras» no son operaciones.
_MONEY = (
    "pagar", "comprar", "transferir", "enviar dinero", "hacer bizum", "confirmar pago", "confirmar compra",
    "finalizar compra", "tramitar pedido", "realizar pedido", "hacer pedido", "suscribirse", "suscribirme",
    "donar", "vender", "ordenar compra", "ordenar venta", "mercar", "pagamento",
    "pay", "pay now", "buy", "buy now", "purchase", "checkout", "check out", "place order", "order now",
    "subscribe", "donate", "transfer", "sell", "confirm payment",
)
_DELETE = (
    "eliminar", "borrar", "suprimir", "dar de baja", "darse de baja", "darme de baja", "anular", "vaciar",
    "cancelar suscripcion", "cancelar pedido", "cancelar reserva", "cancelar cita", "dar de baixa",
    "delete", "remove", "erase", "discard", "empty trash", "unsubscribe", "cancel subscription",
)
_SEND = (
    "enviar", "publicar", "compartir", "solicitar", "reservar", "inscribirse", "inscribirme", "matricularse",
    "matricularme", "firmar", "presentar", "confirmar", "guardar", "responder", "comentar", "pedir cita",
    "gardar", "asinar",
    "send", "post", "publish", "share", "submit", "save", "apply", "sign", "book", "reply", "confirm",
)
# En un sitio de dinero se pregunta por defecto; estos controles no cambian nada.
_HARMLESS = (
    "buscar", "filtrar", "siguiente", "anterior", "ver", "ver mas", "mostrar", "ocultar", "cerrar", "volver",
    "atras", "menu", "ayuda", "ordenar por", "descargar", "imprimir",
    "search", "filter", "next", "previous", "back", "view", "show", "hide", "close", "help", "download", "print",
)


def _pattern(phrases: Sequence[str]) -> re.Pattern[str]:
    return re.compile(r"\b(" + "|".join(re.escape(p) for p in sorted(phrases, key=len, reverse=True)) + r")\b")


_MONEY_RE, _DELETE_RE, _SEND_RE, _HARMLESS_RE = map(_pattern, (_MONEY, _DELETE, _SEND, _HARMLESS))


def _normalize(text: str) -> str:
    plain = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in plain if unicodedata.category(ch) != "Mn").lower()


def classify(kind: str, label: str, context: Mapping[str, Any] | None, *, money_site: bool = False) -> str | None:
    """Categoría de efecto de una acción: `"dinero"`, `"borrar"`, `"enviar"` o None.

    `kind` es `"CLICK"`, `"DOUBLE_CLICK"` o `"ENTER"`. `context` describe el control y su
    formulario (ver `_CONTEXT_JS`); None si no se pudo leer.
    """
    activates = kind in ("CLICK", "DOUBLE_CLICK")
    enter = kind == "ENTER"
    if not activates and not enter:
        return None
    ctx = context or {}
    text = _normalize(label or ctx.get("submitLabel") or "")
    in_form = bool(ctx.get("inForm"))
    submits = in_form and ((activates and ctx.get("submits")) or (enter and ctx.get("textEntry")))
    is_login = bool(ctx.get("password"))
    is_search = bool(ctx.get("search")) or (bool(submits) and ctx.get("method") == "get")

    if _MONEY_RE.search(text):
        return "dinero"
    if _DELETE_RE.search(text):
        return "borrar"
    if money_site:
        button_like = activates and (ctx.get("tag") in ("button", "input") or ctx.get("role") in ("button", "menuitem"))
        routine = submits and (is_login or is_search)
        if (button_like or submits) and not routine and not _HARMLESS_RE.search(text):
            return "dinero"
    if activates and _SEND_RE.search(text):
        return "enviar"
    if submits and not is_login and not is_search:
        return "enviar"
    return None


# Describe el control (por su id de arc-cua, o el que tiene el foco) y su formulario.
_CONTEXT_JS = r"""
((id) => {
  const state = window.__arcCua;
  let el = id ? (state && state.nodes.get(id)) : document.activeElement;
  if (!el || !el.isConnected) return null;
  const control = (el.closest && el.closest("button, input, select, textarea, a[href], [role=button], [role=menuitem], [role=link]")) || el;
  const tag = control.localName;
  const type = ((control.getAttribute && control.getAttribute("type")) || "").toLowerCase();
  const form = control.form || (control.closest && control.closest("form")) || null;
  const text = (s) => (s || "").replace(/\s+/g, " ").trim().slice(0, 80);
  const labelOf = (c) => {
    if (c.labels && c.labels.length) return text(c.labels[0].innerText.replace(c.value || "", ""));
    return text(c.getAttribute("aria-label") || c.getAttribute("placeholder") || c.getAttribute("name") || c.id);
  };
  // Datos de tarjeta: en la aprobación solo se ven las cuatro últimas cifras del número.
  const cardLike = (c) => /^cc-(number|csc)/.test(c.getAttribute("autocomplete") || "") ||
    /card|tarjeta|\bpan\b|cvv|cvc|csc|cvv2|codigo.?seguridad/i.test([c.name, c.id, c.getAttribute("aria-label") || ""].join(" "));
  const valueOf = (c) => {
    const t = (c.getAttribute("type") || "").toLowerCase();
    if (t === "password") return c.value ? "••••" : "";
    const digits = (c.value || "").replace(/[\s-]/g, "");
    if (c.value && (cardLike(c) || /^\d{12,19}$/.test(digits))) return digits.length >= 12 ? "•••• " + digits.slice(-4) : "••••";
    if (t === "checkbox" || t === "radio") return c.checked ? "sí" : "";
    if (c.localName === "select") return text(c.selectedOptions[0] ? c.selectedOptions[0].innerText : "");
    return text(c.value);
  };
  const skip = new Set(["hidden", "submit", "button", "reset", "image", "file"]);
  const fields = form ? [...form.elements]
    .filter((c) => c.localName !== "button" && c.localName !== "fieldset" && !skip.has((c.getAttribute("type") || "").toLowerCase()))
    .map((c) => [labelOf(c), valueOf(c)]).filter(([, v]) => v) : [];
  const submitter = form ? form.querySelector("button:not([type]), button[type=submit], input[type=submit]") : null;
  const textTypes = new Set(["", "text", "search", "email", "url", "tel", "password", "number"]);
  return {
    tag, type,
    role: (control.getAttribute && control.getAttribute("role")) || "",
    inForm: !!form,
    method: form ? (form.getAttribute("method") || "get").toLowerCase() : null,
    search: !!form && (form.getAttribute("role") === "search" || !!form.closest("[role=search]") || !!form.querySelector("input[type=search]")),
    password: !!form && !!form.querySelector("input[type=password]"),
    submits: !!form && ((tag === "button" && (type === "" || type === "submit")) || (tag === "input" && (type === "submit" || type === "image"))),
    textEntry: tag === "input" && textTypes.has(type),
    submitLabel: submitter ? text(submitter.innerText || submitter.value) : "",
    fields,
  };
})
"""


@dataclass(frozen=True)
class ApprovalRequest:
    category: str  # "enviar" | "borrar" | "dinero"
    domain: str
    action: str  # p. ej. "CLICK Publicar"
    fields: tuple[tuple[str, str], ...]
    png: bytes
    fingerprint: str


class Denied(Exception):
    """Pablo no permitió la acción, o la aprobación caducó."""


class Unreadable(Exception):
    """Operación de dinero cuyos datos no se pueden leer de la página: no se pide aprobación."""


class AlreadyActed(Exception):
    """Ya se ejecutó una operación de dinero en esta subtarea: no se hace nada más."""


def _host(url: str) -> str:
    return urlparse(url).hostname or ""


class GatedBackend:
    """Backend de arc-cua que pide aprobación antes de ejecutar una acción con efecto."""

    def __init__(
        self,
        browser: Browser,
        approve: Callable[[ApprovalRequest], str],
        *,
        money_domains: Sequence[str] = (),
        domain_of: Callable[[str], str] = _host,
        on_effect: Callable[[str, str, str, str], None] | None = None,
        max_asks: int = 3,
    ) -> None:
        self._browser = browser
        self._inner = browser.backend
        self._approve = approve
        self._money_domains = tuple(money_domains)
        self._domain_of = domain_of
        self._on_effect = on_effect
        self._max_asks = max_asks
        self.denied: str | None = None
        self.unreadable = False
        self.money_executed = False
        self.paused = 0.0  # segundos esperando la decisión de Pablo

    def __getattr__(self, name: str) -> Any:
        # observe, is_fresh, settle_probe… los resuelve el backend real.
        return getattr(self._inner, name)

    def execute(self, snapshot: Any, action: Any) -> None:
        if self.money_executed:
            raise AlreadyActed("ya se ejecutó una operación de dinero")
        request = self._request_for(snapshot, action)
        if request is not None:
            self._ask(request, snapshot, action)
        self._inner.execute(snapshot, action)
        if request is not None and request.category == "dinero":
            self.money_executed = True

    def _request_for(self, snapshot: Any, action: Any) -> ApprovalRequest | None:
        if action.kind in (ActionKind.CLICK, ActionKind.DOUBLE_CLICK):
            kind = action.kind.value
        elif action.kind == ActionKind.PRESS_KEY and (action.key or "").upper() == "ENTER":
            kind = "ENTER"
        else:
            return None
        label = ""
        if action.target_id:
            try:
                label = snapshot.element(action.target_id).name
            except KeyError:
                label = ""
        context = self._browser._eval(f"{_CONTEXT_JS}({json.dumps(action.target_id)})")
        url = self._browser._eval("location.href") or ""
        host = _host(url)
        money_site = any(host == d or host.endswith("." + d) for d in self._money_domains)
        category = classify(kind, label, context, money_site=money_site)
        if category is None:
            return None
        fields = tuple((str(k), str(v)) for k, v in (context or {}).get("fields", ()))
        shown = label or (context or {}).get("submitLabel") or ""
        if category == "dinero" and not fields:
            self.unreadable = True
            raise Unreadable(f"no se pueden leer los datos de la operación «{shown}»")
        fingerprint = hashlib.sha256(
            json.dumps([url, kind, label, action.target_guard, fields], ensure_ascii=False).encode()
        ).hexdigest()
        return ApprovalRequest(
            category=category,
            domain=self._domain_of(url),
            action=f"{'Intro' if kind == 'ENTER' else 'Pulsar'} «{shown}»".strip(),
            fields=fields,
            png=self._browser.screenshot(),
            fingerprint=fingerprint,
        )

    def _ask(self, request: ApprovalRequest, snapshot: Any, action: Any) -> None:
        for _ in range(self._max_asks):
            waiting = time.monotonic()
            try:
                decision = self._approve(request)
            finally:
                self.paused += time.monotonic() - waiting
            if self._on_effect is not None:
                self._on_effect(request.category, request.domain, request.action, decision)
            if decision != "once":
                self.denied = decision
                raise Denied(decision)
            # La aprobación vale para lo que Pablo vio. Si la página cambió mientras decidía:
            if action.target_id and not self._inner.is_fresh(snapshot, action):
                raise StaleDesktopState("la página cambió durante la aprobación")
            current = self._request_for(snapshot, action)
            if current is not None and current.fingerprint == request.fingerprint:
                return
            if current is None:
                raise StaleDesktopState("la acción dejó de tener efecto tras cambiar la página")
            request = current
        self.denied = "changing"
        raise Denied("la página no deja de cambiar")


class _StopAfterMoney:
    """Tras una operación de dinero no se decide nada más: el planificador comprueba el resultado."""

    def __init__(self, policy: Any, gate: GatedBackend) -> None:
        self._policy = policy
        self._gate = gate

    def decide(self, **kwargs: Any) -> Decision:
        if self._gate.money_executed:
            return Decision(terminal=TerminalKind.NEEDS_AGENT)
        return self._policy.decide(**kwargs)


def run_gated_subtask(
    browser: Browser,
    policy: Any,
    payload: Mapping[str, Any],
    approve: Callable[[ApprovalRequest], str],
    *,
    money_domains: Sequence[str] = (),
    domain_of: Callable[[str], str] = _host,
    on_effect: Callable[[str, str, str, str], None] | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Como `run_subtask`, con la barrera puesta. Añade el estado `DENIED`."""
    gate = GatedBackend(browser, approve, money_domains=money_domains, domain_of=domain_of, on_effect=on_effect)
    result = run_subtask(gate, _StopAfterMoney(policy, gate), payload, paused=lambda: gate.paused, **options)
    if gate.denied is not None:
        result["status"] = "DENIED"
        result["reason"] = "Pablo no permitió la acción." if gate.denied == "deny" else f"Sin aprobación ({gate.denied})."
    elif gate.unreadable:
        result["status"] = "NEEDS_AGENT"
        result["reason"] = "No se pudieron leer de la página los datos de la operación de dinero; no se pidió aprobación."
    elif gate.money_executed:
        result["status"] = "NEEDS_AGENT"
        result["reason"] = "Se ejecutó una operación de dinero. No la repitas: comprueba el resultado en la página."
    return result
