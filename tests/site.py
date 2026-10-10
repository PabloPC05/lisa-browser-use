"""Sitio web de pruebas, servido por las propias pruebas en 127.0.0.1."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


def page(title: str, body: str) -> str:
    return f"<!doctype html><html lang='es'><head><meta charset='utf-8'><title>{title}</title></head><body>{body}</body></html>"


PAGES: dict[str, str] = {
    "/": page(
        "Biblioteca municipal",
        "<h1>Biblioteca municipal</h1><p>Bienvenida a la biblioteca.</p>"
        "<p><a href='/horarios'>Horarios</a> · <a href='/login'>Zona de socios</a></p>",
    ),
    "/horarios": page(
        "Horarios",
        "<h1>Horarios</h1><p>La sala de estudio abre de 9:00 a 21:00 de lunes a viernes.</p>"
        "<p><a href='/'>Volver</a></p>",
    ),
    "/largo": page("Texto largo", "<p>" + "palabra " * 2000 + "</p>"),
    "/login": page(
        "Zona de socios",
        "<h1>Zona de socios</h1><form method='post' action='/entrar'>"
        "<label>Usuario <input id='usuario' name='usuario' type='text'></label>"
        "<label>Contraseña <input id='clave' name='clave' type='password'></label>"
        "<button type='submit'>Entrar</button></form>",
    ),
}


class Site:
    """Servidor HTTP efímero. `with Site() as site: site.url('/horarios')`."""

    def __init__(self, pages: dict[str, str] | None = None) -> None:
        self.pages = dict(PAGES if pages is None else pages)
        self.requests: list[tuple[str, str, bytes]] = []
        site = self

        class Handler(BaseHTTPRequestHandler):
            def _serve(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                site.requests.append((self.command, self.path, body))
                html = site.pages.get(self.path.split("?")[0])
                status = 200 if html is not None else 404
                payload = (html if html is not None else page("No encontrado", "<h1>404</h1>")).encode()
                self.send_response(status)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = _serve

            def log_message(self, *args) -> None:
                pass

        self._server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def url(self, path: str = "/") -> str:
        return f"http://127.0.0.1:{self._server.server_address[1]}{path}"

    def __enter__(self) -> Site:
        self._thread.start()
        return self

    def __exit__(self, *_) -> None:
        self._server.shutdown()
        self._server.server_close()
