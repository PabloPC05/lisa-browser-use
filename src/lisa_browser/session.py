"""Sesión de Chromium: abrir, leer y capturar.

Envuelve el `ChromeBackend` de arc-cua. Lo que arc-cua no ofrece (estado de la
carga, texto completo, captura a demanda) se pide por el mismo canal de
DevTools, a través de su método interno `_call`: por eso la versión de arc-cua
está fijada.
"""

from __future__ import annotations

import base64
from typing import Any

from arc_cua.backends.chrome import ChromeBackend

_READ_JS = "document.body ? document.body.innerText : ''"
_STATUS_JS = (
    "(() => { const n = performance.getEntriesByType('navigation')[0];"
    " return n && typeof n.responseStatus === 'number' ? n.responseStatus : 0; })()"
)


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

    def read(self, max_chars: int = 8000) -> dict[str, Any]:
        """Texto visible de la página entera, para que el planificador extraiga datos."""
        text = self._eval(_READ_JS) or ""
        return {
            "url": self._eval("location.href"),
            "title": self._eval("document.title") or "",
            "text": text[:max_chars],
            "truncated": len(text) > max_chars,
        }

    def screenshot(self) -> bytes:
        """PNG de lo que se ve. Es para Pablo, no para los modelos."""
        return base64.b64decode(self._call("Page.captureScreenshot", {"format": "png"})["data"])

    def _call(self, method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
        return self.backend._call(method, params)

    def _eval(self, expression: str) -> Any:
        result = self._call("Runtime.evaluate", {"expression": expression, "returnByValue": True})
        return result.get("result", {}).get("value")
