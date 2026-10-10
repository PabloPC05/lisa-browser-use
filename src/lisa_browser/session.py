"""Sesión de Chromium: abrir, leer y capturar.

Envuelve el `ChromeBackend` de arc-cua. Lo que arc-cua no ofrece (estado de la
carga, texto completo, captura a demanda) se pide por el mismo canal de
DevTools, a través de su método interno `_call`: por eso la versión de arc-cua
está fijada.
"""

from __future__ import annotations

import base64
import json
import re
import unicodedata
from typing import Any

from arc_cua.backends.chrome import ChromeBackend

_READ_JS = "document.body ? document.body.innerText : ''"
_STATUS_JS = (
    "(() => { const n = performance.getEntriesByType('navigation')[0];"
    " return n && typeof n.responseStatus === 'number' ? n.responseStatus : 0; })()"
)


# Durante la captura, el contenido de los campos de contraseña y de código se vuelve
# transparente: ni siquiera se ve cuántos caracteres hay. Se hace con una hoja de estilos
# construida desde el script, que funciona aunque la página prohíba estilos en línea.
# No alcanza a los marcos de otro origen: ahí queda la ocultación normal del navegador.
_SECRET_FIELDS = (
    "input[type=password]",
    "input[autocomplete~=current-password]",
    "input[autocomplete~=new-password]",
    "input[autocomplete~=one-time-code]",
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
    def __init__(self, backend: ChromeBackend) -> None:
        self.backend = backend

    @classmethod
    def launch(
        cls, *, executable: str | None = None, headless: bool = True, extra_args: tuple[str, ...] = (),
        user_data_dir: str | None = None,
    ) -> Browser:
        """Arranca un Chromium propio. Para pruebas; en producción se usa `connect`."""
        return cls(ChromeBackend.launch(
            headless=headless, executable=executable, extra_args=extra_args, user_data_dir=user_data_dir,
        ))

    @classmethod
    def connect(cls, endpoint: str) -> Browser:
        """Abre una pestaña en el Chromium del contenedor."""
        return cls(ChromeBackend.connect(endpoint))

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
