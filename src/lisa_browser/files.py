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


# Un PDF abierto en el visor del navegador no llega como descarga ni se puede leer como texto.
# Un enlace con `download` hacia la misma dirección lo baja con la sesión de la página.
_SAVE_PDF_JS = (
    "(() => { if (document.contentType !== 'application/pdf' || window.__lisaPdfSaved) return false;"
    " window.__lisaPdfSaved = true;"
    " let name = ''; try { name = decodeURIComponent(location.pathname.split('/').pop() || ''); } catch (e) {}"
    # Con un nombre vacío, Chromium llama «download» al archivo si la dirección lleva tildes codificadas.
    " const a = document.createElement('a'); a.href = location.href; a.download = name || 'documento.pdf';"
    " (document.body || document.documentElement).appendChild(a); a.click(); a.remove(); return true; })()"
)


def _signature(path: Path) -> tuple[int, int]:
    """Fecha y tamaño de un archivo; (0, 0) si desaparece mientras se mira."""
    try:
        stat = path.stat()
    except OSError:
        return (0, 0)
    return (stat.st_mtime_ns, stat.st_size)


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
        # Nombre -> (fecha, tamaño): bajar otra vez un archivo con el mismo nombre lo sobrescribe, y cuenta como nuevo.
        self._seen = {p.name: _signature(p) for p in self.downloads_dir.iterdir()} if self.downloads_dir.exists() else {}
        self._browser.backend._conn.call("Browser.setDownloadBehavior", {
            "behavior": "allow", "downloadPath": str(downloads_dir_in_browser or downloads_dir),
        })

    # --- descargas --------------------------------------------------------------

    def save_shown_pdf(self) -> bool:
        """Si la página es un PDF abierto en el visor, lo descarga. Devuelve si había que hacerlo.

        La marca de «ya bajado» va en el propio documento: mientras siga en pantalla no se repite,
        y volver a abrir la misma dirección (otra petición, otra ejecución de una rutina) lo baja de nuevo.
        """
        try:
            # Con `userGesture`: sin él, Chromium solo deja a una página una descarga automática y
            # bloquea las siguientes, que es justo lo que pasa al pedir el mismo PDF otro día.
            result = self._browser._call(
                "Runtime.evaluate", {"expression": _SAVE_PDF_JS, "returnByValue": True, "userGesture": True}
            )
            return bool(result.get("result", {}).get("value"))
        except Exception:
            return False

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
            found = {p.name: _signature(p) for p in self.downloads_dir.iterdir()}
            partial = [n for n in found if n.endswith(_PARTIAL)]
            fresh = sorted(n for n, mark in found.items() if not n.endswith(_PARTIAL) and self._seen.get(n) != mark)
            if not partial and (fresh or time.monotonic() >= quiet_until):
                break
            if time.monotonic() >= deadline:
                break
            sleep(0.1)
        self._seen.update({n: found[n] for n in fresh})
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
