"""El ejecutor: hace las operaciones del navegador que pide el MCP (mcp_server.py).

Una operación a la vez. Cada operación corre en un hilo, porque arc-cua es
síncrona, y publica eventos que el MCP sigue. Los eventos `approval` y `secret`
esperan una respuesta de Pablo.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import queue
import shutil
import threading
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from .files import Files
from .gate import ApprovalRequest, run_gated_subtask
from .jev import Usage
from .journal import Journal
from .library import Library, MissingParams, substitute
from .login import HAS_PASSWORD_JS, login, save_password
from .runner import Limits
from .session import Browser
from .vault import Vault, registrable_domain

log = logging.getLogger(__name__)

_COLLECT_AFTER = ("open", "follow", "task", "login", "save_password", "routine")


@dataclass(frozen=True)
class Settings:
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
    if op in ("open", "follow"):
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
        saved_downloads_dir: Path | None = None,
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
        # Las descargas se guardan aquí, fuera de la carpeta que ve el navegador, hasta que Pablo diga qué hacer con ellas.
        self.saved_downloads_dir = Path(saved_downloads_dir) if saved_downloads_dir else None

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
                log.warning("la operación %s falló sin conexión con Chromium", operation.op, exc_info=True)
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
                result["downloads"] = self._collect(operation, url)
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
        if op in ("open", "follow"):
            # `follow` abre el enlace que se llama así: lo resuelve el ejecutor mirando la página, sin JEV.
            opened = browser.open(str(args["url"])) if op == "open" else browser.follow(str(args.get("text") or ""))
            # Abrir devuelve ya el principio del texto (o lo que se busca): ahorra una lectura aparte.
            read_chars = int(args.get("read_chars", 1500))
            if opened["ok"] and (read_chars > 0 or find):
                opened.update(_text_of(browser.read(read_chars or 1500, find=find)))
            if opened["ok"] and args.get("links"):
                opened["links"] = browser.links(find)
            return opened
        if op == "read":
            read = browser.read(int(args.get("max_chars", 4000)), find=find, offset=int(args.get("offset", 0)))
            if args.get("links"):
                # Con su dirección, el planificador puede abrir un enlace sin encargar que se pulse.
                read["links"] = browser.links(find)
            return read
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
        def approve(request: ApprovalRequest) -> str:
            answer = operation.ask(
                "approval", self.settings.approval_seconds, category=request.category, domain=request.domain,
                action=request.action, fields=[list(f) for f in request.fields],
                png=base64.b64encode(request.png).decode(),
            )
            return answer if answer in ("once", "deny") else "expired"

        if op == "task":
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
                account=str(args.get("account") or ""),
                # Si la conexión se corta con la contraseña o el código ya enviados, se rehace para ver si se entró.
                reconnect=self._ensure_browser, **options,
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
            label = str(step.get("goal") or step.get("url") or step.get("text") or step.get("find") or op)[:80]
            operation.emit("step", n=n, action=f"rutina {n}/{len(steps)}: {op}", target=label, ms=0)
            if op == "login" and not browser._eval(HAS_PASSWORD_JS):
                # La sesión del sitio se conserva entre tareas: si la página no pide contraseña, ya está dentro.
                continue
            args = {k: v for k, v in step.items() if k != "op"}
            # De un paso intermedio solo interesa su texto si la rutina lo pide con `find`.
            wants_text = op == "read" or "find" in step or n == len(steps)
            if op in ("open", "follow") and not wants_text:
                args["read_chars"] = 0
            result = self._dispatch(operation, journal_id, op, args)
            browser = self._ensure_browser()  # un inicio de sesión cortado pudo rehacer la conexión
            if not _step_ok(op, result):
                status = str(result.get("status") or "failed")
                self.library.ran(name, False, status)
                return {
                    "status": "ROUTINE_FAILED", "routine": routine["name"], "failed_step": n, "of": len(steps),
                    "step": {k: step[k] for k in ("op", "url", "text", "goal") if k in step},
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

    def _collect(self, operation: Operation, url: str = "") -> list[dict]:
        assert self._files is not None
        found = []
        # Un PDF que se quedó abierto en el visor se baja aquí, para que llegue como cualquier descarga.
        pdf_name = self._files.shown_pdf_name()
        showing_pdf = self._files.save_shown_pdf()
        for item in self._files.collect(wait_s=3.0 if showing_pdf else 0.5):
            # Si el sitio manda el nombre del archivo con tildes, el Chromium del contenedor no sabe
            # escribirlo y lo guarda como «download». Se entrega con el nombre que sale de su dirección.
            if pdf_name and "." not in item["name"]:
                item = {**item, "name": pdf_name}
            item = self._archive(item, url)
            download_id = "d_" + uuid.uuid4().hex[:12]
            self._downloads[download_id] = item
            operation.emit("download", id=download_id, name=item["name"], bytes=item["bytes"])
            found.append({"id": download_id, "name": item["name"], "bytes": item["bytes"],
                          **({"saved_as": item["saved_as"]} if "saved_as" in item else {})})
        return found

    def _archive(self, item: dict, url: str) -> dict:
        """Lleva la descarga a la carpeta de descargas guardadas, con una ficha de dónde salió."""
        if self.saved_downloads_dir is None:
            return item
        self.saved_downloads_dir.mkdir(parents=True, exist_ok=True)
        stem = time.strftime("%Y-%m-%d_%H%M%S") + "_" + Path(item["name"]).name
        target, n = self.saved_downloads_dir / stem, 1
        while target.exists():
            n += 1
            target = self.saved_downloads_dir / f"{n}-{stem}"
        shutil.move(item["path"], target)
        card = {"name": item["name"], "bytes": item["bytes"], "from": url,
                "saved_at": datetime.now(timezone.utc).isoformat(timespec="seconds")}
        target.with_name(target.name + ".json").write_text(json.dumps(card, ensure_ascii=False))
        return {**item, "path": str(target), "saved_as": target.name}

    def saved_downloads(self, limit: int = 30) -> list[dict]:
        """Las descargas guardadas, de la más reciente a la más antigua."""
        if self.saved_downloads_dir is None or not self.saved_downloads_dir.is_dir():
            return []
        found = []
        for card in sorted(self.saved_downloads_dir.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)[:limit]:
            try:
                found.append({"file": card.name[:-len(".json")], **json.loads(card.read_text())})
            except ValueError:
                continue
        return found

    def download_path(self, download_id: str) -> Path | None:
        item = self._downloads.get(download_id)
        return Path(item["path"]) if item else None
