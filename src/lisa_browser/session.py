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
import unicodedata
from typing import Any

import httpx
from arc_cua.backends.cdp import CDPConnection, browser_ws_url
from arc_cua.backends.chrome import ChromeBackend

log = logging.getLogger(__name__)

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

    @classmethod
    def launch(
        cls, *, executable: str | None = None, headless: bool = True, extra_args: tuple[str, ...] = (),
        user_data_dir: str | None = None,
    ) -> Browser:
        """Arranca un Chromium propio. Para pruebas; en producción se usa `connect`."""
        backend = ChromeBackend.launch(
            headless=headless, executable=executable, extra_args=extra_args, user_data_dir=user_data_dir,
        )
        return cls(backend, backend._process.ws_url if backend._process is not None else None)

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
        connection = CDPConnection(ws_url)
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
            return base64.b64decode(self._call("Page.captureScreenshot", {"format": "png"})["data"])
        finally:
            self._eval(_SHOW_SECRETS_JS)

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.backend._call(method, params)

    def _eval(self, expression: str) -> Any:
        result = self._call(
            "Runtime.evaluate", {"expression": expression, "returnByValue": True, "awaitPromise": True}
        )
        return result.get("result", {}).get("value")
