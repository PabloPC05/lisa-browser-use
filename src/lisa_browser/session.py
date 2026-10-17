"""Sesión de Chromium: abrir, leer y capturar.

Envuelve el `ChromeBackend` de arc-cua. Lo que arc-cua no ofrece (estado de la
carga, texto completo, captura a demanda) se pide por el mismo canal de
DevTools, a través de su método interno `_call`: por eso la versión de arc-cua
está fijada.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import threading
import time
import unicodedata
from typing import Any, Callable

import httpx
from arc_cua.backends.cdp import CDPConnection, CDPError, ChromeProcess, browser_ws_url
from arc_cua.backends.chrome import ChromeBackend
from websockets.exceptions import ConnectionClosed
from websockets.protocol import State
from websockets.sync.client import connect as ws_connect

log = logging.getLogger(__name__)

# Errores de una orden que solo lee y que se arreglan repitiéndola: la página cambió mientras
# se evaluaba algo en ella.
_NAVIGATED = (
    "Execution context was destroyed",
    "Inspected target navigated or closed",
    "Cannot find default execution context",
    "Cannot find context with specified id",
)


class Connection(CDPConnection):
    """La conexión de arc-cua con Chromium, que no se cae por no leerla y se rehace si se cae.

    websockets deja de leer del socket cuando tiene más de 16 mensajes sin recoger, y entonces
    tampoco lee la respuesta a su ping: a los 20-40 s cierra la conexión con «1011 keepalive
    ping timeout». Una página con actividad (Instagram) manda eventos sin parar mientras el
    ejecutor espera un código o no hace nada; por eso un hilo los recoge cuando nadie usa la
    conexión. Ni una navegación a otro origen ni el cambio de proceso de la pestaña la rompen.

    Si aun así se cae, la orden siguiente abre otra conexión al mismo Chromium, repite la
    configuración del navegador (`_REPLAY`), vuelve a la pestaña con `on_reopen` y se envía
    por la nueva. Como la orden no llegó a salir, sea cual sea no se hace dos veces.
    """

    ping_interval = 20.0
    ping_timeout = 20.0
    #: Cada cuánto se recogen los eventos, y cuánto tiene que llevar la conexión sin usarse.
    drain_every_s = 1.0
    # Órdenes del navegador (sin pestaña) que configuran la conexión y se pierden con ella.
    _REPLAY = ("Target.setDiscoverTargets", "Browser.setDownloadBehavior")

    def __init__(self, ws_url: str, *, timeout_s: float = 15, on_event: Callable | None = None) -> None:
        self.ws_url = ws_url
        self.timeout_s = timeout_s
        self.on_event = on_event
        #: Vuelve a la pestaña tras rehacer la conexión; devuelve la sesión nueva.
        self.on_reopen: Callable[[], str | None] | None = None
        self._next_id = 0
        self._lock = threading.RLock()
        self._setup: dict[str, dict[str, Any]] = {}
        self._used = time.monotonic()
        self._closed = False
        self._open()
        threading.Thread(target=self._keep_reading, name="cdp-drain", daemon=True).start()

    def _open(self) -> None:
        self._connection = ws_connect(
            self.ws_url, max_size=None, compression=None, proxy=None, open_timeout=self.timeout_s,
            ping_interval=self.ping_interval, ping_timeout=self.ping_timeout,
        )
        self._ws = self._connection.__enter__()

    def is_open(self) -> bool:
        return self._ws.protocol.state is State.OPEN

    def call(
        self, method: str, params: dict[str, Any] | None = None, *, session_id: str | None = None, **kwargs: Any,
    ) -> dict[str, Any]:
        with self._lock:
            try:
                if not self._closed and not self.is_open():
                    session_id = self._reopen(session_id)
                if session_id is None and method in self._REPLAY:
                    self._setup[method] = dict(params or {})
                return super().call(method, params, session_id=session_id, **kwargs)
            finally:
                self._used = time.monotonic()

    def drain(self) -> None:
        with self._lock:
            try:
                if not self._closed and not self.is_open():
                    self._reopen(None)
                super().drain()
            finally:
                self._used = time.monotonic()

    def close(self) -> None:
        # Cerrada a propósito no se rehace.
        self._closed = True
        with self._lock:
            super().close()

    def _reopen(self, session_id: str | None) -> str | None:
        log.info("conexión con Chromium caída; se abre otra y se vuelve a la pestaña")
        try:
            self._connection.__exit__(None, None, None)
        except Exception:
            pass
        self._open()
        for method, params in self._setup.items():
            super().call(method, params)
        session = self.on_reopen() if self.on_reopen is not None else None
        # La sesión de la pestaña era de la conexión vieja: la orden va por la nueva.
        return session if session_id is not None else None

    def _keep_reading(self) -> None:
        """Recoge los eventos pendientes cuando la conexión lleva un rato sin usarse.

        Solo entonces: los eventos cambian el estado del backend de arc-cua (diálogo abierto,
        peticiones en curso), y mientras trabaja ese estado solo debe cambiar en su hilo.
        """
        while not self._closed:
            time.sleep(self.drain_every_s)
            if time.monotonic() - self._used < self.drain_every_s or not self._lock.acquire(blocking=False):
                continue
            try:
                if not self._closed and self.is_open():
                    CDPConnection.drain(self)
            except Exception:
                log.debug("no se pudieron recoger los eventos de Chromium", exc_info=True)
            finally:
                self._lock.release()

_READ_JS = "document.body ? document.body.innerText : ''"
# Enlaces que se ven, con su texto en una línea. Solo los que llevan a una dirección web.
_LINKS_JS = (
    "[...document.querySelectorAll('a[href]')].filter((a) => a.getClientRects().length > 0)"
    ".map((a) => ({ text: (a.innerText || a.getAttribute('aria-label') || a.title || '').replace(/\\s+/g, ' ').trim(),"
    " url: a.href })).filter((l) => l.text && /^https?:/.test(l.url))"
)
_STATUS_JS = (
    "(() => { const n = performance.getEntriesByType('navigation')[0];"
    " return n && typeof n.responseStatus === 'number' ? n.responseStatus : 0; })()"
)


# Durante la captura, el contenido de los campos de contraseña, de código y de tarjeta se vuelve
# transparente: ni siquiera se ve cuántos caracteres hay. Se hace con una hoja de estilos
# construida desde el script, que funciona aunque la página prohíba estilos en línea.
# No alcanza a los marcos de otro origen: ahí queda la ocultación normal del navegador.
_SECRET_FIELDS = (
    "input[type=password]",
    "input[autocomplete~=current-password]",
    "input[autocomplete~=new-password]",
    "input[autocomplete~=one-time-code]",
    # Tarjetas de pago (card.py).
    "input[autocomplete^=cc-number]",
    "input[autocomplete^=cc-csc]",
    "input[name*=card i]", "input[id*=card i]",
    "input[name*=tarjeta i]", "input[id*=tarjeta i]",
    "input[name*=cvv i]", "input[id*=cvv i]",
    "input[name*=cvc i]", "input[id*=cvc i]",
)
_HIDDEN = (
    "color: transparent !important; -webkit-text-fill-color: transparent !important; "
    "text-shadow: none !important; caret-color: transparent !important;"
)
_HIDE_SECRETS_CSS = "\n".join(
    f"{field}, {field}::placeholder, {field}::selection {{ {_HIDDEN} }}" for field in _SECRET_FIELDS
)
_HIDE_SECRETS_JS = """
(() => {
  if (window.__lisaHide) return true;  // ya ocultos: se puede repetir sin dejar una hoja de más
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(%s);
  window.__lisaHide = sheet;
  document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  return new Promise(done => requestAnimationFrame(() => requestAnimationFrame(() => done(true))));
})()
""" % json.dumps(_HIDE_SECRETS_CSS)
_SHOW_SECRETS_JS = """
(() => {
  document.adoptedStyleSheets = document.adoptedStyleSheets.filter(sheet => sheet !== window.__lisaHide);
  delete window.__lisaHide;
  return true;
})()
"""


def _plain(text: str) -> str:
    normal = unicodedata.normalize("NFD", text)
    return "".join(ch for ch in normal if unicodedata.category(ch) != "Mn").lower()


def _around(line: str, terms: list[str], width: int = 300) -> str:
    """Una línea muy larga se recorta alrededor de lo que se busca."""
    if len(line) <= 2 * width:
        return line
    plain = _plain(line)
    at = min((plain.find(t) for t in terms if t in plain), default=0)
    start, end = max(0, at - width), min(len(line), at + width)
    return ("…" if start else "") + line[start:end] + ("…" if end < len(line) else "")


def excerpt(text: str, *, max_chars: int = 4000, find: str = "", offset: int = 0) -> dict[str, Any]:
    """La parte de `text` que hace falta leer: desde `offset`, o solo lo que contiene `find`.

    `find` admite varios términos separados por comas; no distingue mayúsculas ni tildes.
    Cada coincidencia va con la línea anterior y la siguiente.
    """
    text = re.sub(r"[ \t\xa0]+\n", "\n", text.replace("\r", ""))
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    max_chars = max(0, int(max_chars))
    terms = [_plain(t.strip()) for t in find.split(",") if t.strip()]
    if not terms:
        start = max(0, int(offset))
        part = text[start:start + max_chars]
        out: dict[str, Any] = {"text": part, "truncated": start + max_chars < len(text), "total_chars": len(text)}
        if out["truncated"]:
            out["next_offset"] = start + max_chars
        return out

    lines = [line for line in text.split("\n") if line.strip()]
    hits = [i for i, line in enumerate(lines) if any(t in _plain(line) for t in terms)]
    keep: list[int] = []
    for i in hits:
        for j in (i - 1, i, i + 1):
            if 0 <= j < len(lines) and j not in keep:
                keep.append(j)
    keep.sort()
    parts: list[str] = []
    used, previous, truncated = 0, None, False
    for j in keep:
        line = _around(lines[j], terms)
        gap = "…\n" if previous is not None and j != previous + 1 else ""
        if used + len(gap) + len(line) + 1 > max_chars:
            truncated = True
            break
        parts.append(gap + line)
        used += len(gap) + len(line) + 1
        previous = j
    return {"text": "\n".join(parts), "matches": len(hits), "truncated": truncated, "total_chars": len(text)}


class Browser:
    def __init__(self, backend: ChromeBackend, ws_url: str | None = None) -> None:
        self.backend = backend
        #: Dirección WebSocket del navegador, para abrirle otra conexión (navegador en vivo).
        self.ws_url = ws_url
        if isinstance(backend._conn, Connection):
            # Si la conexión se rehace, este mismo backend vuelve a la pestaña: quien lo tenga
            # en la mano (gate.py, el planificador) sigue usándolo sin enterarse.
            backend._conn.on_reopen = self._reattach

    @classmethod
    def launch(
        cls, *, executable: str | None = None, headless: bool = True, extra_args: tuple[str, ...] = (),
        user_data_dir: str | None = None,
    ) -> Browser:
        """Arranca un Chromium propio. Para pruebas; en producción se usa `connect`.

        Es `ChromeBackend.launch` con la conexión de aquí (`Connection`).
        """
        process = ChromeProcess(
            executable=executable, headless=headless, user_data_dir=user_data_dir, extra_args=extra_args,
        )
        try:
            connection = Connection(process.ws_url)
            pages = [t for t in connection.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]
            target_id = pages[0]["targetId"] if pages else connection.call(
                "Target.createTarget", {"url": "about:blank"},
            )["targetId"]
            return cls(ChromeBackend(connection, target_id, process=process), process.ws_url)
        except BaseException:
            process.close()
            raise

    @classmethod
    def connect(cls, endpoint: str, *, locale: str = "", timezone: str = "") -> Browser:
        """Se une al Chromium del contenedor y sigue en la pestaña en la que estaba.

        Reutiliza la pestaña usada más recientemente y cierra las demás: una reconexión
        no pierde la página abierta ni va dejando pestañas olvidadas.
        """
        base = endpoint.rstrip("/")
        # Chromium lista las pestañas de la más reciente a la más antigua.
        pages = [t for t in httpx.get(base + "/json/list", timeout=5).json() if t.get("type") == "page"]
        ws_url = browser_ws_url(base)
        connection = Connection(ws_url)
        if pages:
            target_id = pages[0]["id"]
            for stale in pages[1:]:
                try:
                    connection.call("Target.closeTarget", {"targetId": stale["id"]})
                except Exception:
                    log.debug("no se pudo cerrar la pestaña %s", stale.get("id"), exc_info=True)
        else:
            target_id = connection.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        browser = cls(ChromeBackend(connection, target_id), ws_url)
        browser.present_as_desktop(locale=locale, timezone=timezone)
        return browser

    def present_as_desktop(self, *, locale: str = "", timezone: str = "") -> None:
        """Hace que la pestaña se presente como un Chromium de escritorio corriente.

        Un Chromium sin ventana se anuncia como «HeadlessChrome» y usa el idioma y la hora del
        servidor: se quita esa marca y se fijan el idioma y la zona horaria de Pablo. Se repite
        en cada pestaña a la que pase la sesión.
        """
        attach = self.backend._attach

        def attach_and_present(target_id: str) -> None:
            attach(target_id)
            self._present(locale, timezone)

        self.backend._attach = attach_and_present  # type: ignore[method-assign]
        self._present(locale, timezone)

    def _present(self, locale: str, timezone: str) -> None:
        agent = self._eval("navigator.userAgent") or ""
        if "Headless" in agent:
            version = re.search(r"Chrome/(\d+)[\d.]*", agent)
            full, major = (version.group(0)[7:], version.group(1)) if version else ("", "")
            brands = [{"brand": "Chromium", "version": major}, {"brand": "Not_A Brand", "version": "24"}]
            override: dict[str, Any] = {
                "userAgent": agent.replace("HeadlessChrome", "Chrome"),
                "userAgentMetadata": {
                    "brands": brands,
                    "fullVersionList": [{**brands[0], "version": full}, {**brands[1], "version": "24.0.0.0"}],
                    "platform": "Linux", "platformVersion": "", "architecture": "x86", "bitness": "64",
                    "model": "", "mobile": False,
                },
            }
            if locale:
                override["acceptLanguage"] = f"{locale},{locale.split('-')[0]};q=0.9"
            self._call("Emulation.setUserAgentOverride", override)
        if locale:
            try:
                self._call("Emulation.setLocaleOverride", {"locale": locale})
            except Exception as exc:
                # Chromium guarda un solo idioma para todo el navegador: al pasar a otra pestaña
                # (un enlace que abre una nueva) ya está puesto, y repetirlo es un error.
                if "already in effect" not in str(exc):
                    raise
        if timezone:
            self._call("Emulation.setTimezoneOverride", {"timezoneId": timezone})

    def close(self) -> None:
        self.backend.close()

    def __enter__(self) -> Browser:
        return self

    def __exit__(self, *_: Any) -> None:
        self.close()

    def open(self, url: str) -> dict[str, Any]:
        """Navega a `url`. No lanza excepción si la página no existe o no carga: lo indica."""
        try:
            self.backend.navigate(url)
        except RuntimeError as exc:
            return {"ok": False, "url": url, "title": "", "status": None, "error": str(exc)}
        status = int(self._eval(_STATUS_JS) or 0)
        return {
            # Sin código (0) es una página que no viene de HTTP, como about:blank.
            "ok": status == 0 or 200 <= status < 400,
            "url": self._eval("location.href"),
            "title": self._eval("document.title") or "",
            "status": status or None,
            "error": None if status == 0 or status < 400 else f"HTTP {status}",
        }

    def read(self, max_chars: int = 4000, *, find: str = "", offset: int = 0) -> dict[str, Any]:
        """Texto visible de la página, para que el planificador extraiga datos.

        Con `find` devuelve solo los fragmentos que lo contienen; con `offset` sigue leyendo.
        """
        return {
            "url": self._eval("location.href"),
            "title": self._eval("document.title") or "",
            **excerpt(self._eval(_READ_JS) or "", max_chars=max_chars, find=find, offset=offset),
        }

    def links(self, find: str = "", limit: int = 15) -> list[dict[str, str]]:
        """Enlaces visibles de la página con su dirección. Con `find`, los que contienen alguno de sus términos.

        Con la dirección en la mano, el planificador abre el enlace él mismo: no hace falta
        que un modelo lo busque en la pantalla y lo pulse.
        """
        terms = [_plain(term.strip()) for term in find.split(",") if term.strip()]
        seen: set[tuple[str, str]] = set()
        out: list[dict[str, str]] = []
        for link in self._eval(_LINKS_JS) or []:
            key = (link["text"], link["url"])
            if key in seen or (terms and not any(term in _plain(link["text"]) for term in terms)):
                continue
            seen.add(key)
            out.append({"text": link["text"][:120], "url": link["url"]})
        return out[:limit]

    def follow(self, text: str) -> dict[str, Any]:
        """Abre el enlace de la página que se llama `text`. No decide ningún modelo.

        Gana el enlace cuyo texto es exactamente ese; si no hay, el que empieza así; si no, el
        que lo contiene. Si en el mejor de esos grupos hay enlaces a sitios distintos no se
        elige: se devuelven para que el planificador abra el que quiere por su dirección.
        """
        wanted = _plain(" ".join(text.split()))
        if not wanted:
            return {"ok": False, "error": "no_link", "detail": "falta el texto del enlace"}
        groups: dict[int, list[dict[str, str]]] = {0: [], 1: [], 2: []}
        for link in self.links(limit=1000):
            plain = _plain(link["text"])
            if plain == wanted:
                groups[0].append(link)
            elif plain.startswith(wanted):
                groups[1].append(link)
            elif wanted in plain:
                groups[2].append(link)
        for matches in groups.values():
            if not matches:
                continue
            if len({link["url"].split("#")[0] for link in matches}) == 1:
                return {**self.open(matches[0]["url"]), "followed": matches[0]["text"]}
            return {
                "ok": False, "error": "ambiguous_link",
                "detail": "varios enlaces encajan: abre uno por su dirección o da su texto completo",
                "links": matches[:8],
            }
        return {"ok": False, "error": "no_link", "detail": "ningún enlace visible tiene ese texto"}

    def screenshot(self) -> bytes:
        """PNG de lo que se ve, con los campos de contraseña y de código en blanco. Es para Pablo, no para los modelos."""
        self._eval(_HIDE_SECRETS_JS)
        try:
            return base64.b64decode(self._call("Page.captureScreenshot", {"format": "png"}, retry=True)["data"])
        finally:
            self._eval(_SHOW_SECRETS_JS)

    def _reattach(self) -> str | None:
        """Tras rehacer la conexión, vuelve a la misma pestaña; si ya no existe, a la que haya."""
        backend = self.backend
        pages = [t["targetId"] for t in backend._conn.call("Target.getTargets")["targetInfos"] if t["type"] == "page"]
        if backend.target_id in pages:
            target_id = backend.target_id
        elif pages:
            target_id = pages[0]
        else:
            target_id = backend._conn.call("Target.createTarget", {"url": "about:blank"})["targetId"]
        # Con `present_as_desktop`, esto también vuelve a presentar la pestaña como de escritorio.
        backend._attach(target_id)
        return backend.session_id

    def _call(self, method: str, params: dict[str, Any] | None = None, *, retry: bool = False) -> dict[str, Any]:
        """Una orden de DevTools a la pestaña.

        Una orden que no llegó a salir porque la conexión estaba caída se envía por una nueva
        (`Connection`). Con `retry`, que es solo para órdenes que se pueden repetir sin efecto
        (leer, capturar), también se repite una vez si la conexión cae esperando la respuesta
        o si la página cambió mientras tanto; antes se espera a que termine de cargar.
        """
        try:
            return self.backend._call(method, params)
        except (ConnectionClosed, CDPError) as exc:
            if not retry or not (isinstance(exc, ConnectionClosed) or any(m in str(exc) for m in _NAVIGATED)):
                raise
            log.info("%s falló (%s); se repite", method, exc)
        time.sleep(0.1)
        self.backend._wait_loaded()
        return self.backend._call(method, params)

    def _eval(self, expression: str) -> Any:
        """Evalúa `expression` en la página. Solo para leer o para lo que se puede repetir: puede ir dos veces."""
        result = self._call(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}, retry=True,
        )
        return result.get("result", {}).get("value")
