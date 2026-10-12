"""API del ejecutor (PLAN §4.2): el puente pide operaciones y recibe sus eventos.

Una operación a la vez. Cada operación corre en un hilo, porque arc-cua es
síncrona, y publica eventos que la petición HTTP devuelve línea a línea. Los
eventos `approval` y `secret` esperan una respuesta del puente.
"""

from __future__ import annotations

import asyncio
import base64
import hmac
import json
import logging
import queue
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from aiohttp import web

from .files import Files
from .gate import ApprovalRequest, run_gated_subtask
from .jev import Usage
from .journal import Journal
from .library import Library, LibraryError, MissingParams, substitute
from .login import HAS_PASSWORD_JS, login, save_password
from .runner import Limits
from .session import Browser
from .vault import Vault, registrable_domain

log = logging.getLogger(__name__)

OPS = (
    "open", "task", "read", "login", "save_password", "attach", "screenshot", "sessions", "logout", "log", "routine",
)
_COLLECT_AFTER = ("open", "task", "login", "save_password", "routine")


@dataclass(frozen=True)
class Settings:
    token: str
    money_domains: tuple[str, ...] = ()
    limits: Limits = Limits()
    approval_seconds: float = 600.0
    code_seconds: float = 300.0
    price_per_mtok: float = 0.0


@dataclass
class Operation:
    conversation: str | None
    op: str
    args: dict[str, Any]
    loop: asyncio.AbstractEventLoop
    id: str = field(default_factory=lambda: "op_" + uuid.uuid4().hex[:12])
    events: asyncio.Queue = field(default_factory=asyncio.Queue)
    cancel: threading.Event = field(default_factory=threading.Event)
    _answers: dict[str, queue.Queue] = field(default_factory=dict)
    _count: int = 0

    def emit(self, event_type: str, /, **fields: Any) -> str:
        self._count += 1
        event_id = f"e{self._count}"
        self.loop.call_soon_threadsafe(self.events.put_nowait, {"type": event_type, "event": event_id, **fields})
        return event_id

    def ask(self, event_type: str, timeout: float, /, **fields: Any) -> Any | None:
        """Publica un evento que espera respuesta. None si caduca o se cancela."""
        waiting: queue.Queue = queue.Queue(maxsize=1)
        self._count += 1
        event_id = f"e{self._count}"
        self._answers[event_id] = waiting
        self.loop.call_soon_threadsafe(self.events.put_nowait, {"type": event_type, "event": event_id, **fields})
        deadline = time.monotonic() + timeout
        try:
            while time.monotonic() < deadline and not self.cancel.is_set():
                try:
                    return waiting.get(timeout=0.1)
                except queue.Empty:
                    continue
            return None
        finally:
            self._answers.pop(event_id, None)

    def answer(self, event_id: str, value: Any) -> bool:
        waiting = self._answers.get(event_id)
        if waiting is None:
            return False
        waiting.put_nowait(value)
        return True


def _brief(page: Any) -> dict[str, Any] | None:
    """De la observación final de arc-cua, solo dónde ha quedado la página."""
    if not isinstance(page, dict):
        return None
    context = page.get("context") or {}
    return {
        "url": context.get("url"), "title": context.get("title") or page.get("window"),
        "elements": len(page.get("elements") or ()), "more_below": bool(context.get("more_below")),
    }


def _step_ok(op: str, result: dict[str, Any]) -> bool:
    """Si un paso de una rutina salió como para seguir con el siguiente."""
    if op == "open":
        return bool(result.get("ok"))
    if op == "task":
        return result.get("status") == "SUBTASK_COMPLETE"
    if op == "login":
        return result.get("status") == "ok"
    return True


def _text_of(read: dict[str, Any]) -> dict[str, Any]:
    return {k: read[k] for k in ("text", "matches", "truncated", "total_chars", "next_offset") if k in read}


class Executor:
    """Navegador, política de decisión, baúl, archivos y registro compartidos por las operaciones."""

    def __init__(
        self, *,
        settings: Settings,
        connect: Callable[[], Browser],
        policy_for: Callable[[Operation], Any],
        journal: Journal,
        files_for: Callable[[Browser], Files],
        uploads_dir: Path,
        vault: Vault | None = None,
        usage: Usage | None = None,
        library: Library | None = None,
    ) -> None:
        self.settings = settings
        self._connect = connect
        self._policy_for = policy_for
        self._journal = journal
        self._files_for = files_for
        self.uploads_dir = Path(uploads_dir)
        self._vault = vault
        self._usage = usage
        self.library = library
        self._browser: Browser | None = None
        self._files: Files | None = None
        self._lock = threading.Lock()
        self._waiting = 0
        self._downloads: dict[str, dict] = {}

    # --- ciclo de una operación -----------------------------------------------------

    def run(self, operation: Operation) -> None:
        if not self._lock.acquire(blocking=False):
            self._waiting += 1
            operation.emit("queued", position=self._waiting)
            try:
                while not self._lock.acquire(timeout=0.1):
                    if operation.cancel.is_set():
                        operation.emit("done", result={"status": "CANCELLED", "reason": "Cancelado en la cola."})
                        return
            finally:
                self._waiting -= 1
        try:
            operation.emit("done", result=self._run_locked(operation))
        finally:
            self._lock.release()

    def _run_locked(self, operation: Operation) -> dict[str, Any]:
        goal = (operation.args.get("goal") or operation.args.get("url") or operation.args.get("domain")
                or operation.args.get("name"))
        journal_id = self._journal.start(operation.conversation, operation.op, goal)
        used_before = self._usage.copy() if self._usage else Usage()
        started = time.monotonic()
        try:
            result = self._dispatch(operation, journal_id)
        except Exception as exc:
            if self._browser_is_dead():
                # Chromium dejó de responder: la siguiente operación vuelve a conectar.
                self._browser = self._files = None
                result = {"status": "ERROR", "reason": "browser_unavailable"}
            else:
                log.exception("la operación %s falló", operation.op)
                result = {"status": "ERROR", "reason": type(exc).__name__}
        spent = self._usage.since(used_before) if self._usage else Usage()
        sites = []
        if self._browser is not None and operation.op in _COLLECT_AFTER:
            try:
                url = self._browser._eval("location.href") or ""
                sites = [registrable_domain(url)]
                if self.library is not None and result.get("status") not in ("ERROR", "CANCELLED"):
                    self.library.visited(url)
                result["downloads"] = self._collect(operation)
            except Exception:
                log.debug("no se pudo cerrar la operación con la página", exc_info=True)
        self._journal.finish(
            journal_id, str(result.get("status", "ok")), round(time.monotonic() - started, 2),
            input_tokens=spent.input_tokens, cost_usd=round(spent.cost_usd(self.settings.price_per_mtok), 6),
            sites=[s for s in sites if s],
        )
        return result

    def _dispatch(
        self, operation: Operation, journal_id: str, op: str | None = None, args: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Hace la operación pedida o, con `op` y `args`, un paso de una rutina."""
        op, args = (operation.op, operation.args) if op is None else (op, args or {})
        if op == "routine":
            return self._run_routine(operation, journal_id)
        if op == "log":
            return {"status": "ok", "operations": self._journal.summary(args.get("since"))}
        browser = self._ensure_browser()
        files = self._files
        assert files is not None

        def on_step(step: dict) -> None:
            operation.emit("step", n=step["n"], action=step["action"], target=step["target"], ms=step["ms"])
            self._journal.step(journal_id, step["n"], step["action"], None, step["target"], step["ms"], "jev")

        find = str(args.get("find") or "")
        if op == "open":
            opened = browser.open(str(args["url"]))
            # Abrir devuelve ya el principio del texto (o lo que se busca): ahorra una lectura aparte.
            read_chars = int(args.get("read_chars", 1500))
            if opened["ok"] and (read_chars > 0 or find):
                opened.update(_text_of(browser.read(read_chars or 1500, find=find)))
            return opened
        if op == "read":
            return browser.read(int(args.get("max_chars", 4000)), find=find, offset=int(args.get("offset", 0)))
        if op == "screenshot":
            operation.emit("screenshot", png=base64.b64encode(browser.screenshot()).decode())
            return {"status": "ok", "sent": True}
        if op == "attach":
            return files.attach(str(args["file"]), str(args.get("field", "")))
        if op == "sessions":
            with_cookies = set(files.cookie_domains())
            return {"status": "ok", "sessions": [s for s in self._journal.sessions() if s["domain"] in with_cookies]}
        if op == "logout":
            domain = str(args.get("domain") or "all")
            closed = files.logout(domain)
            self._journal.session_closed(None if domain == "all" else registrable_domain(domain))
            return {"status": "ok", "closed": closed}
        if op == "task":
            def approve(request: ApprovalRequest) -> str:
                answer = operation.ask(
                    "approval", self.settings.approval_seconds, category=request.category, domain=request.domain,
                    action=request.action, fields=[list(f) for f in request.fields],
                    png=base64.b64encode(request.png).decode(),
                )
                return answer if answer in ("once", "deny") else "expired"

            payload = {k: args[k] for k in ("goal", "verification", "inputs", "constraints", "max_actions") if k in args}
            result = run_gated_subtask(
                browser, self._policy_for(operation), payload, approve,
                money_domains=self.settings.money_domains, domain_of=registrable_domain,
                on_effect=lambda *effect: self._journal.effect(journal_id, *effect),
                on_step=on_step, cancel=operation.cancel, usage=self._usage,
                price_per_mtok=self.settings.price_per_mtok, limits=self.settings.limits,
            )
            # El planificador no necesita la lista de elementos que ya ha usado JEV:
            # recibe dónde ha quedado la página y, si lo pide, el texto que busca.
            result["page"] = _brief(result.get("page"))
            if find:
                result.update(_text_of(browser.read(int(args.get("read_chars", 1500)), find=find)))
            return result
        if op in ("login", "save_password"):
            if self._vault is None:
                return {"status": "vault_unavailable", "reason": "no hay baúl configurado"}
            options: dict[str, Any] = {}
            if op == "save_password":
                # La contraseña nueva la escribe Pablo en el chat: va del puente al ejecutor sin pasar por el agente.
                options["ask_password"] = lambda domain, username, timeout: operation.ask(
                    "secret", timeout, kind="password", domain=domain, username=username,
                )
            result = (save_password if op == "save_password" else login)(
                browser, self._policy_for(operation), self._vault,
                ask_code=lambda domain, timeout: operation.ask("secret", timeout, kind="code", domain=domain),
                on_secret=lambda value: self._journal.add_secret(journal_id, value),
                on_step=on_step, code_seconds=self.settings.code_seconds, cancel=operation.cancel, usage=self._usage,
                account=str(args.get("account") or ""), **options,
            )
            if result["status"] == "ok":
                self._journal.session_opened(result["domain"])
            return result
        raise ValueError(f"operación desconocida: {op}")

    def _run_routine(self, operation: Operation, journal_id: str) -> dict[str, Any]:
        """Hace seguidos los pasos de una rutina guardada. El planificador solo ve el final.

        Cada paso es una operación normal: JEV decide las acciones y la barrera sigue pidiendo
        permiso para enviar, borrar o pagar. Si un paso no sale, la rutina se detiene ahí y
        devuelve dónde ha quedado, para que el planificador siga a mano y la corrija.
        """
        name = str(operation.args.get("name") or "")
        if self.library is None:
            return {"status": "ERROR", "reason": "library_unavailable"}
        routine = self.library.routine(name)
        if routine is None:
            return {"status": "no_routine", "name": name, "routines": [r["name"] for r in self.library.routines()]}
        try:
            steps = substitute(routine["steps"], operation.args.get("params") or {})
        except MissingParams as exc:
            return {"status": "NEEDS_INPUT", "routine": routine["name"], "missing": exc.missing}

        browser = self._ensure_browser()
        outputs: list[dict[str, Any]] = []
        for n, step in enumerate(steps, 1):
            if operation.cancel.is_set():
                self.library.ran(name, False, "CANCELLED")
                return {"status": "CANCELLED", "routine": routine["name"], "stopped_at": n, "of": len(steps)}
            op = step["op"]
            label = str(step.get("goal") or step.get("url") or step.get("find") or op)[:80]
            operation.emit("step", n=n, action=f"rutina {n}/{len(steps)}: {op}", target=label, ms=0)
            if op == "login" and not browser._eval(HAS_PASSWORD_JS):
                # La sesión del sitio se conserva entre tareas: si la página no pide contraseña, ya está dentro.
                continue
            args = {k: v for k, v in step.items() if k != "op"}
            # De un paso intermedio solo interesa su texto si la rutina lo pide con `find`.
            wants_text = op == "read" or "find" in step or n == len(steps)
            if op == "open" and not wants_text:
                args["read_chars"] = 0
            result = self._dispatch(operation, journal_id, op, args)
            if not _step_ok(op, result):
                status = str(result.get("status") or "failed")
                self.library.ran(name, False, status)
                return {
                    "status": "ROUTINE_FAILED", "routine": routine["name"], "failed_step": n, "of": len(steps),
                    "step": {k: step[k] for k in ("op", "url", "goal") if k in step},
                    "result": {k: v for k, v in result.items() if k not in ("metrics", "text", "matches")},
                    "url": browser._eval("location.href"), "title": browser._eval("document.title"),
                }
            text = _text_of(result) if wants_text else {}
            if text:
                outputs.append({"step": n, **text})
        self.library.ran(name, True, "ok")
        done: dict[str, Any] = {
            "status": "ok", "routine": routine["name"], "steps": len(steps),
            "url": browser._eval("location.href"), "title": browser._eval("document.title"),
        }
        if len(outputs) == 1:
            done.update({k: v for k, v in outputs[0].items() if k != "step"})
        elif outputs:
            done["results"] = outputs
        return done

    def _ensure_browser(self) -> Browser:
        if self._browser is not None and self._browser_is_dead():
            # La conexión con Chromium se pierde tras un rato sin uso: se rehace antes de empezar,
            # cuando todavía no se ha hecho nada de la operación.
            log.info("conexión con Chromium perdida; se vuelve a conectar")
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = self._files = None
        if self._browser is None:
            self._browser = self._connect()
            self._files = self._files_for(self._browser)
        return self._browser

    def _browser_is_dead(self) -> bool:
        if self._browser is None:
            return True
        try:
            return self._browser._eval("1") != 1
        except Exception:
            return True

    def _collect(self, operation: Operation) -> list[dict]:
        assert self._files is not None
        found = []
        # Un PDF que se quedó abierto en el visor se baja aquí, para que llegue como cualquier descarga.
        showing_pdf = self._files.save_shown_pdf()
        for item in self._files.collect(wait_s=3.0 if showing_pdf else 0.5):
            download_id = "d_" + uuid.uuid4().hex[:12]
            self._downloads[download_id] = item
            operation.emit("download", id=download_id, name=item["name"], bytes=item["bytes"])
            found.append({"id": download_id, "name": item["name"], "bytes": item["bytes"]})
        return found

    def download_path(self, download_id: str) -> Path | None:
        item = self._downloads.get(download_id)
        return Path(item["path"]) if item else None

    def journal_summary(self, since: str | None) -> list[dict]:
        return self._journal.summary(since)


def make_app(executor: Executor) -> web.Application:
    pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="operacion")
    operations: dict[str, Operation] = {}
    expected = f"Bearer {executor.settings.token}"

    @web.middleware
    async def auth(request: web.Request, handler: Any) -> web.StreamResponse:
        if not hmac.compare_digest(request.headers.get("Authorization", "").encode(), expected.encode()):
            return web.json_response({"error": "unauthorized"}, status=401)
        return await handler(request)

    async def start(request: web.Request) -> web.StreamResponse:
        try:
            body = await request.json()
            op, args = body["op"], body.get("args") or {}
            if op not in OPS or not isinstance(args, dict):
                raise ValueError
        except (ValueError, KeyError, TypeError):
            return web.json_response({"error": "invalid_request", "ops": list(OPS)}, status=400)
        operation = Operation(body.get("conversation"), op, args, asyncio.get_running_loop())
        operations[operation.id] = operation
        response = web.StreamResponse(headers={"Content-Type": "application/x-ndjson"})
        await response.prepare(request)
        await response.write(json.dumps({"type": "started", "operation": operation.id}).encode() + b"\n")
        work = asyncio.get_running_loop().run_in_executor(pool, executor.run, operation)
        try:
            while True:
                event = await operation.events.get()
                await response.write(json.dumps(event, ensure_ascii=False).encode() + b"\n")
                if event["type"] == "done":
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            operation.cancel.set()  # el puente se fue: no se sigue actuando a ciegas
            raise
        finally:
            operations.pop(operation.id, None)
            await asyncio.shield(work)
        await response.write_eof()
        return response

    async def answer(request: web.Request) -> web.Response:
        operation = operations.get(request.match_info["id"])
        body = await request.json()
        if operation is None or not operation.answer(str(body.get("event")), body.get("value")):
            return web.json_response({"error": "not_waiting"}, status=404)
        return web.json_response({"ok": True})

    async def cancel(request: web.Request) -> web.Response:
        operation = operations.get(request.match_info["id"])
        if operation is None:
            return web.json_response({"error": "not_found"}, status=404)
        operation.cancel.set()
        return web.json_response({"ok": True})

    async def upload(request: web.Request) -> web.Response:
        name = request.match_info["name"]
        if Path(name).name != name or name.startswith("."):
            return web.json_response({"error": "invalid_name"}, status=400)
        target = executor.uploads_dir / name
        with open(target, "wb") as out:
            async for chunk in request.content.iter_chunked(1 << 20):
                out.write(chunk)
        return web.json_response({"ok": True, "name": name, "bytes": target.stat().st_size})

    async def download(request: web.Request) -> web.StreamResponse:
        path = executor.download_path(request.match_info["id"])
        if path is None or not path.is_file():
            return web.json_response({"error": "not_found"}, status=404)
        return web.FileResponse(path)

    # --- biblioteca: sitios habituales y rutinas -------------------------------------
    # No pasan por la cola de operaciones: no tocan el navegador y deben responder
    # aunque haya una tarea en curso.

    def with_library(handler: Callable[[web.Request, Library], Any]) -> Any:
        async def wrapped(request: web.Request) -> web.Response:
            if executor.library is None:
                return web.json_response({"error": "library_unavailable"}, status=404)
            try:
                return web.json_response(await handler(request, executor.library))
            except LibraryError as exc:
                return web.json_response({"error": "invalid", "reason": str(exc)}, status=400)
            except (ValueError, TypeError, AttributeError):
                return web.json_response({"error": "invalid_request"}, status=400)
        return wrapped

    async def library_list(_: web.Request, library: Library) -> dict:
        return library.listing()

    async def site_put(request: web.Request, library: Library) -> dict:
        body = await request.json()
        return library.save_site(request.match_info["name"], body.get("url"), body.get("notes"), body.get("aliases"))

    async def site_delete(request: web.Request, library: Library) -> dict:
        return {"deleted": library.delete_site(request.match_info["name"])}

    async def routine_get(request: web.Request, library: Library) -> dict:
        found = library.routine(request.match_info["name"])
        if found is None:
            raise LibraryError("no hay ninguna rutina con ese nombre")
        return found

    async def routine_put(request: web.Request, library: Library) -> dict:
        body = await request.json()
        return library.save_routine(request.match_info["name"], body.get("description"), body.get("steps"))

    async def routine_delete(request: web.Request, library: Library) -> dict:
        return {"deleted": library.delete_routine(request.match_info["name"])}

    async def journal(request: web.Request) -> web.Response:
        return web.json_response({"operations": executor.journal_summary(request.query.get("since"))})

    app = web.Application(middlewares=[auth], client_max_size=4 * 1024**3)
    app.add_routes([
        web.post("/v1/operations", start),
        web.post("/v1/operations/{id}/answers", answer),
        web.post("/v1/operations/{id}/cancel", cancel),
        web.put("/v1/uploads/{name}", upload),
        web.get("/v1/downloads/{id}", download),
        web.get("/v1/journal", journal),
        web.get("/v1/library", with_library(library_list)),
        web.put("/v1/library/sites/{name}", with_library(site_put)),
        web.delete("/v1/library/sites/{name}", with_library(site_delete)),
        web.get("/v1/library/routines/{name}", with_library(routine_get)),
        web.put("/v1/library/routines/{name}", with_library(routine_put)),
        web.delete("/v1/library/routines/{name}", with_library(routine_delete)),
    ])

    async def close(_: web.Application) -> None:
        for operation in list(operations.values()):
            operation.cancel.set()
        pool.shutdown(wait=False, cancel_futures=True)

    app.on_cleanup.append(close)
    return app
