"""Navegador en vivo (lisa-assistant D86): lo que pinta la pestaña de Lisa, para mirarlo.

Chrome emite fotogramas de la pestaña con `Page.startScreencast`. La conexión de arc-cua
es síncrona y solo recoge eventos mientras espera una respuesta, así que aquí se abre otra,
asíncrona, a la misma pestaña en la que trabaja el ejecutor. No pasa por la cola de
operaciones: mirar no espera a una tarea ni la retrasa.

Una sola emisión para todos los que miran: arranca con el primero y se para con el último.
Los fotogramas no se guardan en ningún sitio.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import AsyncIterator, Callable
from typing import Any

import aiohttp

log = logging.getLogger(__name__)

#: Pestaña que enseñar: la dirección WebSocket del navegador y el identificador de la pestaña.
Source = Callable[[], "tuple[str, str] | None"]

_IDLE_RETRY_S = 2.0
_CHECK_TARGET_S = 1.0


class Screencast:
    def __init__(
        self,
        source: Source,
        *,
        max_fps: float = 5.0,
        quality: int = 60,
        max_size: tuple[int, int] = (1280, 900),
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._source = source
        self._interval = 1.0 / max_fps
        self._quality = quality
        self._max_size = max_size
        self._clock = clock
        self._subscribers: set[asyncio.Queue] = set()
        self._last: dict[str, Any] | None = None
        self._task: asyncio.Task | None = None

    @property
    def watching(self) -> int:
        return len(self._subscribers)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    @contextlib.asynccontextmanager
    async def watch(self) -> AsyncIterator[asyncio.Queue]:
        """Una cola con los eventos para quien mira (`frame`, `idle`) mientras dura el `async with`.

        Es una cola y no un generador para que esperarla con un plazo (el `ping`) no la cierre.
        """
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        self._subscribers.add(queue)
        if self._last is not None:
            queue.put_nowait(self._last)
        if not self.running:
            self._task = asyncio.create_task(self._produce())
        try:
            yield queue
        finally:
            self._subscribers.discard(queue)
            if not self._subscribers and self._task is not None:
                self._task.cancel()
                self._task = None

    def _publish(self, event: dict[str, Any]) -> None:
        if event["type"] == "frame":
            self._last = event
        for queue in self._subscribers:
            # Quien va con retraso se queda con lo más reciente: sobra lo que ya no se ve.
            if queue.full():
                queue.get_nowait()
            queue.put_nowait(event)

    async def _produce(self) -> None:
        async with aiohttp.ClientSession() as session:
            while self._subscribers:
                source = self._source()
                if source is None:
                    self._publish({"type": "idle"})
                    await asyncio.sleep(_IDLE_RETRY_S)
                    continue
                try:
                    await self._stream(session, *source)
                except asyncio.CancelledError:
                    raise
                except Exception:
                    log.info("emisión del navegador interrumpida; se vuelve a enganchar", exc_info=True)
                    await asyncio.sleep(_IDLE_RETRY_S)

    async def _stream(self, session: aiohttp.ClientSession, ws_url: str, target_id: str) -> None:
        """Emite la pestaña `target_id` hasta que cambie la pestaña, se corte o nadie mire."""
        async with session.ws_connect(ws_url, max_msg_size=0) as ws:
            ids = iter(range(1, 1 << 30))

            async def call(method: str, params: dict[str, Any], session_id: str | None = None) -> int:
                message: dict[str, Any] = {"id": next(ids), "method": method, "params": params}
                if session_id is not None:
                    message["sessionId"] = session_id
                await ws.send_str(json.dumps(message))
                return message["id"]

            attach = await call("Target.attachToTarget", {"targetId": target_id, "flatten": True})
            session_id: str | None = None
            pending: dict[str, Any] | None = None
            sent_at = float("-inf")
            checked_at = self._clock()

            while self._subscribers:
                try:
                    message = await ws.receive(timeout=min(_CHECK_TARGET_S, self._interval))
                except asyncio.TimeoutError:
                    message = None
                if message is not None:
                    if message.type in (aiohttp.WSMsgType.CLOSED, aiohttp.WSMsgType.CLOSING, aiohttp.WSMsgType.ERROR):
                        return
                    data = json.loads(message.data)
                    if data.get("id") == attach:
                        if "error" in data:
                            return
                        session_id = data["result"]["sessionId"]
                        width, height = self._max_size
                        await call("Page.startScreencast", {
                            "format": "jpeg", "quality": self._quality, "maxWidth": width, "maxHeight": height,
                        }, session_id)
                    elif data.get("method") == "Page.screencastFrame" and data.get("sessionId") == session_id:
                        params = data["params"]
                        # Chrome no manda el siguiente hasta que se le confirma este.
                        await call("Page.screencastFrameAck", {"sessionId": params["sessionId"]}, session_id)
                        meta = params.get("metadata", {})
                        pending = {
                            "type": "frame", "data": params["data"],
                            "width": round(meta.get("deviceWidth", 0)), "height": round(meta.get("deviceHeight", 0)),
                        }
                    elif data.get("method") in ("Target.detachedFromTarget", "Inspector.detached"):
                        return
                now = self._clock()
                # Como mucho `max_fps`; lo que se descarta se sustituye por el más reciente, que sale en cuanto toca.
                if pending is not None and now - sent_at >= self._interval:
                    self._publish(pending)
                    pending, sent_at = None, now
                if now - checked_at >= _CHECK_TARGET_S:
                    checked_at = now
                    current = self._source()
                    if current is None or current[1] != target_id:
                        return
