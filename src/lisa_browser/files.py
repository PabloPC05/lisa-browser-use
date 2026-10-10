"""Lo que arc-cua no cubre: subir archivos, recoger descargas y cerrar sesiones por sitio."""

from __future__ import annotations

import time
import unicodedata
from pathlib import Path
from typing import Any, Callable

from .session import Browser
from .vault import registrable_domain

_PARTIAL = (".crdownload", ".tmp")
_STORAGE = "local_storage,session_storage,indexeddb,cache_storage,service_workers"

# Devuelve los campos de archivo de la página con el texto que los identifica.
_FILE_INPUTS_JS = r"""
[...document.querySelectorAll("input[type=file]")].map((el, index) => {
  const label = el.labels && el.labels.length ? el.labels[0].innerText : "";
  return { index, text: [label, el.getAttribute("aria-label"), el.name, el.id].filter(Boolean).join(" ") };
})
"""


def _plain(text: str) -> str:
    normal = unicodedata.normalize("NFD", text or "")
    return "".join(ch for ch in normal if unicodedata.category(ch) != "Mn").lower().strip()


class Files:
    def __init__(
        self, browser: Browser, downloads_dir: str | Path, uploads_dir: str | Path, *,
        uploads_dir_in_browser: str | Path | None = None, downloads_dir_in_browser: str | Path | None = None,
    ) -> None:
        self._browser = browser
        self.downloads_dir = Path(downloads_dir)
        self.uploads_dir = Path(uploads_dir)
        # En el contenedor, Chromium ve estas carpetas en otra ruta.
        self._uploads_in_browser = Path(uploads_dir_in_browser or uploads_dir)
        self._seen = {p.name for p in self.downloads_dir.iterdir()} if self.downloads_dir.exists() else set()
        self._browser.backend._conn.call("Browser.setDownloadBehavior", {
            "behavior": "allow", "downloadPath": str(downloads_dir_in_browser or downloads_dir),
        })

    # --- subidas ----------------------------------------------------------------

    def attach(self, name: str, field: str = "") -> dict[str, Any]:
        """Pone el archivo `name` de la carpeta de subidas en el campo de archivo que diga `field`."""
        if Path(name).name != name or not (self.uploads_dir / name).is_file():
            return {"status": "no_file", "file": name}
        inputs = self._browser._eval(_FILE_INPUTS_JS) or []
        wanted = _plain(field)
        # Sin texto, solo vale si la página tiene un único campo de archivo.
        matching = [i for i in inputs if wanted in _plain(i["text"])] if wanted else inputs
        if len(matching) != 1:
            return {"status": "no_field" if not matching else "ambiguous_field", "fields": [i["text"] for i in inputs]}
        handle = self._browser._call("Runtime.evaluate", {
            "expression": f"document.querySelectorAll('input[type=file]')[{int(matching[0]['index'])}]",
        })["result"]
        self._browser._call("DOM.setFileInputFiles", {
            "objectId": handle["objectId"], "files": [str(self._uploads_in_browser / name)],
        })
        return {"status": "ok", "file": name, "field": matching[0]["text"]}

    # --- descargas ----------------------------------------------------------------

    def collect(self, *, wait_s: float = 2.0, timeout_s: float = 120.0, sleep: Callable[[float], None] = time.sleep) -> list[dict]:
        """Descargas terminadas desde la última llamada. Espera a las que estén en curso."""
        deadline = time.monotonic() + timeout_s
        quiet_until = time.monotonic() + wait_s
        while True:
            names = {p.name for p in self.downloads_dir.iterdir()}
            partial = [n for n in names if n.endswith(_PARTIAL)]
            fresh = sorted(n for n in names - self._seen if not n.endswith(_PARTIAL))
            if not partial and (fresh or time.monotonic() >= quiet_until):
                break
            if time.monotonic() >= deadline:
                break
            sleep(0.1)
        self._seen |= set(fresh)
        return [
            {"name": n, "path": str(self.downloads_dir / n), "bytes": (self.downloads_dir / n).stat().st_size}
            for n in fresh
        ]

    # --- sesiones -------------------------------------------------------------------

    def cookie_domains(self) -> list[str]:
        """Dominios registrables que tienen cookies guardadas en el navegador."""
        cookies = self._browser.backend._conn.call("Storage.getCookies").get("cookies", [])
        return sorted({registrable_domain(c["domain"].lstrip(".")) for c in cookies})

    def logout(self, domain: str) -> list[str]:
        """Borra cookies y almacenamiento de `domain`, o de todo con `"all"`. Devuelve lo cerrado."""
        conn = self._browser.backend._conn
        everything = domain == "all"
        target = None if everything else registrable_domain(domain)
        cookies = conn.call("Storage.getCookies").get("cookies", [])
        mine = [c for c in cookies if everything or registrable_domain(c["domain"].lstrip(".")) == target]
        closed = {registrable_domain(c["domain"].lstrip(".")) for c in mine}
        if everything:
            conn.call("Storage.clearCookies")
        else:
            for cookie in mine:
                self._browser._call("Network.deleteCookies", {
                    "name": cookie["name"], "domain": cookie["domain"], "path": cookie["path"],
                })
        # El almacenamiento de la página abierta también puede guardar la sesión.
        here = self._browser._eval("location.origin") or ""
        if here.startswith("http") and (everything or registrable_domain(here) == target):
            if self._browser._eval("localStorage.length + sessionStorage.length") or 0:
                closed.add(registrable_domain(here))
            self._browser._call("Storage.clearDataForOrigin", {"origin": here, "storageTypes": _STORAGE})
        return sorted(closed)
