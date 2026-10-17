"""Inicio de sesión con las contraseñas del baúl y segundo factor (PLAN §6).

El planificador nunca ve la contraseña: aquí se busca en el baúl por el dominio
de la página, se entrega a arc-cua como entrada secreta y se escribe una sola vez.
"""

from __future__ import annotations

import dataclasses
import logging
import re
import time
from typing import Any, Callable, Mapping
from urllib.parse import urlparse

from arc_cua import ActionKind

from .runner import Limits, run_subtask
from .session import Browser
from .vault import Entry, Vault, VaultUnavailable, registrable_domain

log = logging.getLogger(__name__)

CODE_SECONDS = 300
CODE_ATTEMPTS = 2

_CODE_FIELD = re.compile(r"c[oó]digo|code|otp|verificaci[oó]n|sms|token|2fa|autenticaci[oó]n|clave temporal", re.I)
HAS_PASSWORD_JS = "[...document.querySelectorAll('input[type=password]')].some((e) => e.offsetParent !== null)"
# Muchos sitios (Amazon entre ellos) no marcan el campo con autocomplete=one-time-code: se mira también su nombre.
# Otros (Instagram) solo lo dicen en la etiqueta, «Código»; un código postal o promocional no cuenta.
_HAS_CODE_JS = (
    "[...document.querySelectorAll('input:not([type=hidden]):not([type=password])')].some((e) => {"
    " if (e.offsetParent === null) return false;"
    " if (e.autocomplete === 'one-time-code') return true;"
    " if ([e.name, e.id].some((s) => /^(code|c[oó]digo)$|otp|one.?time|verif|2fa|mfa/i.test(s))) return true;"
    " const label = [e.getAttribute('aria-label'), e.placeholder, ...[...(e.labels || [])].map((l) => l.textContent)].join(' ');"
    " return /c[oó]digo|\\bcode\\b|verificaci[oó]n|verification/i.test(label)"
    "   && !/postal|promo|descuento|cup[oó]n|regalo|referido|referral|coupon|zip/i.test(label);"
    "})"
)
#: Tras enviar la contraseña, cuánto se espera a que la web cambie de página: algunas (Instagram) piden
#: el código en otra página que llega un momento después.
SETTLE_SECONDS = 4.0

# ask_code(domain, timeout_seconds) -> código, o None si Pablo no contesta a tiempo.
AskCode = Callable[[str, float], str | None]
# ask_password(domain, username, timeout_seconds) -> contraseña nueva, o None si Pablo no contesta a tiempo.
AskPassword = Callable[[str, str, float], str | None]
# reconnect() -> el navegador, con la conexión rehecha si se perdió.
Reconnect = Callable[[], Browser]


class _SecondAttempt(Exception):
    pass


class _WrongField(Exception):
    pass


def _is_password_field(snapshot: Any, target_id: str | None) -> bool:
    """Si el elemento al que va la acción es un campo `input type=password`."""
    for element in getattr(snapshot, "elements", ()) or ():
        if element.id == target_id:
            return (element.metadata or {}).get("type") == "password"
    return False


class _TypeOnce:
    """Backend que deja escribir la contraseña una sola vez, para no bloquear la cuenta.

    Anota también si ya se escribió la contraseña o el código: desde entonces, un corte de la
    conexión no dice nada de si se entró, y hay que mirar cómo quedó la página.
    """

    def __init__(self, inner: Any, password: str) -> None:
        self._inner = inner
        self.password = password
        self.code = ""
        self.typed = False
        self.code_typed = False
        self.second_attempt = False
        self.wrong_field = False

    @property
    def sent(self) -> bool:
        return self.typed or self.code_typed

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def execute(self, snapshot: Any, action: Any) -> None:
        if action.kind == ActionKind.TYPE_TEXT:
            value = str(action.value)
            to_password_field = _is_password_field(snapshot, action.target_id)
            if to_password_field and value != self.password and not (self.code and value == self.code):
                # Qué secreto va en el campo de contraseña lo decide el código, no el modelo: con el
                # usuario oculto como [secret] en la etiqueta («Escribe la contraseña para [secret]»)
                # podía escribir el usuario donde iba la contraseña.
                action = dataclasses.replace(action, value=self.password)
                value = self.password
            if value == self.password and not to_password_field:
                # La contraseña nunca va a un campo a la vista (usuario, búsqueda, un mensaje...).
                self.wrong_field = True
                raise _WrongField()
            if value == self.password:
                if self.typed:
                    self.second_attempt = True
                    raise _SecondAttempt()
                self.typed = True
            elif self.code and value == self.code:
                self.code_typed = True
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
    reconnect: Reconnect | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Devuelve `{status, domain, reason?}`.

    `status`: `ok`, `no_credentials`, `wrong_domain`, `rejected`, `vault_unavailable` o
    `choose_account`. Este último llega con `accounts` cuando el baúl tiene varias cuentas
    para el sitio: no se escribe nada hasta que `account` (parte del usuario o del nombre
    de la entrada) señala una sola.

    Si la conexión con Chromium se corta después de escribir la contraseña o el código, se
    vuelve a conectar con `reconnect` y se mira la página: `ok` con `recovered` si ya no pide
    nada, o `ERROR` con `asks` (`password` o `code`) si sigue pidiéndolo.
    """
    entry, domain, early = _entry_for(browser, vault, account)
    if entry is None:
        return early
    # Si la web ya pide el código (la contraseña se envió antes, p. ej. en un intento que pasó a otra
    # página), no se vuelve a escribir la contraseña: se pide el código.
    step = _second_factor if not _page_has(browser, HAS_PASSWORD_JS) and _asks_for_code(browser, {}) else _sign_in
    return _guarded(
        step, browser, policy, vault, entry, entry.password, domain, reconnect=reconnect, ask_code=ask_code,
        on_secret=on_secret, on_step=on_step, limits=limits, code_seconds=code_seconds, **options,
    )


def save_password(
    browser: Browser,
    policy: Any,
    vault: Vault,
    *,
    ask_password: AskPassword,
    ask_code: AskCode,
    on_secret: Callable[[str], None] | None = None,
    on_step: Callable[[dict], None] | None = None,
    limits: Limits = Limits(max_actions=12),
    code_seconds: float = CODE_SECONDS,
    account: str = "",
    reconnect: Reconnect | None = None,
    **options: Any,
) -> dict[str, Any]:
    """Prueba una contraseña nueva que da Pablo y, solo si entra, la guarda en su entrada del baúl.

    Devuelve lo mismo que `login` y, con `status: ok`, `saved` (y `reason` si no se pudo guardar).
    La entrada tiene que existir ya: aquí no se crean cuentas ni se cambian direcciones.
    """
    entry, domain, early = _entry_for(browser, vault, account)
    if entry is None:
        return early
    password = ask_password(domain, entry.username, code_seconds)
    if not password:
        return {"status": "rejected", "domain": domain, "reason": "password_timeout"}
    result = _guarded(
        _sign_in, browser, policy, vault, entry, password, domain, reconnect=reconnect, ask_code=ask_code,
        on_secret=on_secret, on_step=on_step, limits=limits, code_seconds=code_seconds, **options,
    )
    if result["status"] != "ok":
        return result
    try:
        vault.set_password(entry, password)
    except VaultUnavailable as exc:
        return {**result, "saved": False, "reason": str(exc)}
    return {**result, "saved": True}


def _entry_for(browser: Browser, vault: Vault, account: str) -> tuple[Entry | None, str, dict[str, Any]]:
    """La entrada del baúl para la página abierta, o el resultado que explica por qué no hay una sola."""
    url = browser._eval("location.href") or ""
    domain = registrable_domain(url)
    try:
        entries = [e for e in vault.find(url) if e.matches(url)]
        if not entries:
            return None, domain, _without_entry(browser, vault, url, domain)
    except VaultUnavailable as exc:
        return None, domain, {"status": "vault_unavailable", "domain": domain, "reason": str(exc)}

    wanted = account.strip().lower()
    if wanted:
        named = [e for e in entries if wanted in e.username.lower() or wanted in e.name.lower()]
        exact = [e for e in named if wanted == e.username.lower()]
        entries = exact or named or entries
    if len(entries) > 1:
        # Solo hay un intento de contraseña: no se adivina la cuenta.
        return None, domain, {
            "status": "choose_account", "domain": domain,
            "accounts": [{"name": e.name, "username": e.username} for e in entries],
        }
    return entries[0], domain, {}


def _guarded(
    step: Callable[..., dict[str, Any]], browser: Browser, policy: Any, vault: Vault, entry: Entry, password: str,
    domain: str, *, reconnect: Reconnect | None, **kwargs: Any,
) -> dict[str, Any]:
    """Hace `step` (`_sign_in` o `_second_factor`) sin dar por fallido un inicio de sesión que solo se cortó.

    Al pasar a la página siguiente (en Instagram, a otro proceso) la conexión con Chromium puede
    perderse con la contraseña o el código ya enviados y la sesión iniciada.
    """
    backend = _TypeOnce(browser.backend, password)
    try:
        result = step(browser, backend, policy, vault, entry, domain, **kwargs)
    except Exception:
        if not backend.sent:
            raise
        log.warning("el inicio de sesión en %s se cortó tras escribir la contraseña o el código", domain, exc_info=True)
        return _after_cut(browser, domain, reconnect)
    if result["status"] != "ok" and backend.sent and not _page_has(browser, "true"):
        # arc-cua se traga algunos fallos de conexión, y el paso acaba en un rechazo que no lo es.
        log.warning("el inicio de sesión en %s acabó sin conexión con Chromium", domain)
        return _after_cut(browser, domain, reconnect)
    return result


def _after_cut(browser: Browser, domain: str, reconnect: Reconnect | None) -> dict[str, Any]:
    """Tras un corte, la página dice si la sesión quedó iniciada. Solo se mira: no se vuelve a escribir nada."""
    if reconnect is not None:
        browser = reconnect()
    deadline = time.monotonic() + SETTLE_SECONDS
    while not _page_has(browser, "document.readyState === 'complete'") and time.monotonic() < deadline:
        time.sleep(0.5)
    try:
        asks_password = bool(browser._eval(HAS_PASSWORD_JS))
        asks_code = not asks_password and bool(browser._eval(_HAS_CODE_JS))
    except Exception:
        return {"status": "ERROR", "reason": "browser_unavailable", "domain": domain}
    if asks_password or asks_code:
        return {
            "status": "ERROR", "reason": "browser_unavailable", "domain": domain,
            "asks": "password" if asks_password else "code",
        }
    return {"status": "ok", "domain": domain, "recovered": True}


def _sign_in(
    browser: Browser, backend: _TypeOnce, policy: Any, vault: Vault, entry: Entry, domain: str, *,
    ask_code: AskCode, on_secret: Callable[[str], None] | None, on_step: Callable[[dict], None] | None,
    limits: Limits, code_seconds: float, **options: Any,
) -> dict[str, Any]:
    password = backend.password
    if on_secret is not None:
        on_secret(password)
    result = run_subtask(
        backend, policy,
        {
            "goal": "Iniciar sesión con el usuario y la contraseña dados.",
            "verification": ["La sesión está iniciada: la página ya no pide la contraseña."],
            "inputs": {"usuario": entry.username, "contraseña": password},
            "secret_inputs": ["usuario", "contraseña"],
            "constraints": ["Escribe la contraseña una sola vez.", "No pulses enlaces de recuperar contraseña."],
        },
        limits=limits, on_step=on_step, **options,
    )
    if backend.second_attempt:
        return {"status": "rejected", "domain": domain, "reason": "password"}

    # La web puede tardar en pasar a la página siguiente: se mira un rato si pide un código antes de
    # decidir, para no dar por iniciada una sesión que espera el segundo factor.
    deadline = time.monotonic() + SETTLE_SECONDS
    while True:
        if _asks_for_code(browser, result):
            return _second_factor(
                browser, backend, policy, vault, entry, domain, ask_code=ask_code, on_secret=on_secret,
                on_step=on_step, limits=limits, code_seconds=code_seconds, **options,
            )
        if time.monotonic() >= deadline:
            break
        time.sleep(0.5)
    if backend.typed and not _page_has(browser, HAS_PASSWORD_JS, default=True):
        return {"status": "ok", "domain": domain}
    if not backend.typed:
        # No es que la web rechazara la contraseña: no se llegó a escribir (no encontró el campo,
        # o quiso escribirla donde no tocaba). Se puede volver a intentar sin gastar el intento.
        reason = "password_wrong_field" if backend.wrong_field else "password_not_typed"
        return {"status": "rejected", "domain": domain, "reason": reason}
    return {"status": "rejected", "domain": domain, "reason": "password"}


def _page_has(browser: Browser, script: str, *, default: bool = False) -> bool:
    """Evalúa una comprobación en la página; mientras cambia de página puede fallar, y vale `default`."""
    try:
        return bool(browser._eval(script))
    except Exception:
        return default


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
    return _page_has(browser, _HAS_CODE_JS)


def _second_factor(
    browser: Browser, backend: _TypeOnce, policy: Any, vault: Vault, entry: Entry, domain: str, *,
    ask_code: AskCode, on_secret: Callable[[str], None] | None, on_step: Callable[[dict], None] | None,
    limits: Limits, code_seconds: float, **options: Any,
) -> dict[str, Any]:
    for _ in range(CODE_ATTEMPTS):
        code = vault.totp(entry) if entry.has_totp else ask_code(domain, code_seconds)
        if not code:
            return {"status": "rejected", "domain": domain, "reason": "code_timeout"}
        if on_secret is not None:
            on_secret(code)
        backend.code = code
        result = run_subtask(
            backend, policy,
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
