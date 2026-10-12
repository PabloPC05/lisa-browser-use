"""Inicio de sesión con las contraseñas del baúl y segundo factor (PLAN §6).

El planificador nunca ve la contraseña: aquí se busca en el baúl por el dominio
de la página, se entrega a arc-cua como entrada secreta y se escribe una sola vez.
"""

from __future__ import annotations

import re
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from arc_cua import ActionKind

from .runner import Limits, run_subtask
from .session import Browser
from .vault import Entry, Vault, VaultUnavailable, registrable_domain

CODE_SECONDS = 300
CODE_ATTEMPTS = 2

_CODE_FIELD = re.compile(r"c[oó]digo|code|otp|verificaci[oó]n|sms|token|2fa|autenticaci[oó]n|clave temporal", re.I)
HAS_PASSWORD_JS = "[...document.querySelectorAll('input[type=password]')].some((e) => e.offsetParent !== null)"
_HAS_CODE_JS = "[...document.querySelectorAll('input[autocomplete=one-time-code]')].some((e) => e.offsetParent !== null)"

# ask_code(domain, timeout_seconds) -> código, o None si Pablo no contesta a tiempo.
AskCode = Callable[[str, float], str | None]


class _SecondAttempt(Exception):
    pass


class _TypeOnce:
    """Backend que deja escribir la contraseña una sola vez, para no bloquear la cuenta."""

    def __init__(self, inner: Any, secret: str) -> None:
        self._inner = inner
        self._secret = secret
        self.typed = False
        self.second_attempt = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def execute(self, snapshot: Any, action: Any) -> None:
        if action.kind == ActionKind.TYPE_TEXT and str(action.value) == self._secret:
            if self.typed:
                self.second_attempt = True
                raise _SecondAttempt()
            self.typed = True
        self._inner.execute(snapshot, action)


def login(
    browser: Browser,
    policy: Any,
    vault: Vault,
    *,
    ask_code: AskCode,
    on_secret: Callable[[str], None] | None = None,
    on_step: Callable[[dict], None] | None = None,
    limits: Limits = Limits(max_actions=12),
    code_seconds: float = CODE_SECONDS,
    account: str = "",
    **options: Any,
) -> dict[str, Any]:
    """Devuelve `{status, domain, reason?}`.

    `status`: `ok`, `no_credentials`, `wrong_domain`, `rejected`, `vault_unavailable` o
    `choose_account`. Este último llega con `accounts` cuando el baúl tiene varias cuentas
    para el sitio: no se escribe nada hasta que `account` (parte del usuario o del nombre
    de la entrada) señala una sola.
    """
    url = browser._eval("location.href") or ""
    domain = registrable_domain(url)
    try:
        entries = [e for e in vault.find(url) if e.matches(url)]
        if not entries:
            return _without_entry(browser, vault, url, domain)
    except VaultUnavailable as exc:
        return {"status": "vault_unavailable", "domain": domain, "reason": str(exc)}

    wanted = account.strip().lower()
    if wanted:
        named = [e for e in entries if wanted in e.username.lower() or wanted in e.name.lower()]
        exact = [e for e in named if wanted == e.username.lower()]
        entries = exact or named or entries
    if len(entries) > 1:
        # Solo hay un intento de contraseña: no se adivina la cuenta.
        return {
            "status": "choose_account", "domain": domain,
            "accounts": [{"name": e.name, "username": e.username} for e in entries],
        }
    entry = entries[0]
    if on_secret is not None:
        on_secret(entry.password)
    backend = _TypeOnce(browser.backend, entry.password)
    result = run_subtask(
        backend, policy,
        {
            "goal": "Iniciar sesión con el usuario y la contraseña dados.",
            "verification": ["La sesión está iniciada: la página ya no pide la contraseña."],
            "inputs": {"usuario": entry.username, "contraseña": entry.password},
            "secret_inputs": ["usuario", "contraseña"],
            "constraints": ["Escribe la contraseña una sola vez.", "No pulses enlaces de recuperar contraseña."],
        },
        limits=limits, on_step=on_step, **options,
    )
    if backend.second_attempt:
        return {"status": "rejected", "domain": domain, "reason": "password"}

    if _asks_for_code(browser, result):
        return _second_factor(
            browser, policy, vault, entry, domain, ask_code=ask_code, on_secret=on_secret, on_step=on_step,
            limits=limits, code_seconds=code_seconds, **options,
        )
    if backend.typed and not browser._eval(HAS_PASSWORD_JS):
        return {"status": "ok", "domain": domain}
    return {"status": "rejected", "domain": domain, "reason": "password"}


def _without_entry(browser: Browser, vault: Vault, url: str, domain: str) -> dict[str, Any]:
    """Sin contraseña para este dominio. Si la página se parece a un sitio guardado, avisa del posible engaño."""
    if browser._eval(HAS_PASSWORD_JS):
        title = (browser._eval("document.title") or "").lower()
        # Palabras del nombre de la página, sin la terminación (.com, .test…), que comparten sitios sin relación.
        host_labels = (urlparse(url).hostname or "").lower().split(".")[:-1]
        words = {w for label in host_labels for w in re.split(r"[^a-z0-9]+", label) if len(w) >= 3}
        for word in sorted(words, key=len, reverse=True)[:3]:
            for entry in vault.search(word):
                expected = next((registrable_domain(u) for u in entry.uris), "")
                if expected and expected != domain and (word in expected or entry.name.lower() in title):
                    return {"status": "wrong_domain", "domain": domain, "expected": expected}
    return {"status": "no_credentials", "domain": domain}


def _asks_for_code(browser: Browser, result: Mapping[str, Any]) -> bool:
    field = result.get("needs_input") or {}
    if result.get("status") == "NEEDS_INPUT" and _CODE_FIELD.search(str(field.get("name") or "")):
        return True
    return bool(browser._eval(_HAS_CODE_JS))


def _second_factor(
    browser: Browser, policy: Any, vault: Vault, entry: Entry, domain: str, *,
    ask_code: AskCode, on_secret: Callable[[str], None] | None, on_step: Callable[[dict], None] | None,
    limits: Limits, code_seconds: float, **options: Any,
) -> dict[str, Any]:
    for _ in range(CODE_ATTEMPTS):
        code = vault.totp(entry) if entry.has_totp else ask_code(domain, code_seconds)
        if not code:
            return {"status": "rejected", "domain": domain, "reason": "code_timeout"}
        if on_secret is not None:
            on_secret(code)
        result = run_subtask(
            browser.backend, policy,
            {
                "goal": "Introducir el código de verificación y continuar.",
                "verification": ["La página ya no pide el código de verificación."],
                "inputs": {"codigo": code},
                "secret_inputs": ["codigo"],
            },
            limits=limits, on_step=on_step, **options,
        )
        if result["status"] == "SUBTASK_COMPLETE" and not _asks_for_code(browser, result):
            return {"status": "ok", "domain": domain}
    return {"status": "rejected", "domain": domain, "reason": "code"}
