"""Navegador en vivo (lisa-assistant D86): la pestaña de Lisa emitida a quien mira."""

from __future__ import annotations

import asyncio
import base64
import json
import unittest

from lisa_browser.screencast import Screencast
from lisa_browser.server import SCREENCAST
from tests.test_server import AUTH, ServerCase


class LiveTest(ServerCase):
    async def events(self, until, *, timeout: float = 20) -> list[dict]:
        """Lee `/v1/screencast` hasta que `until(eventos)` se cumple."""
        seen: list[dict] = []
        async with self.client.get("/v1/screencast", headers=AUTH) as response:
            self.assertEqual(response.status, 200)

            async def read() -> None:
                async for raw in response.content:
                    seen.append(json.loads(raw))
                    if until(seen):
                        return

            await asyncio.wait_for(read(), timeout)
        return seen

    @staticmethod
    def frames(events: list[dict]) -> list[dict]:
        return [event for event in events if event["type"] == "frame"]

    async def test_needs_the_token(self) -> None:
        async with self.client.get("/v1/screencast") as response:
            self.assertEqual(response.status, 401)

    async def test_without_a_browser_yet_it_says_idle(self) -> None:
        events = await self.events(lambda seen: len(seen) >= 1)
        self.assertEqual(events[0], {"type": "idle"})

    async def test_shows_the_tab_and_follows_it_when_the_page_changes(self) -> None:
        self.assertTrue(self.done(await self.op("open", {"url": self.site.url("/horarios")}))["ok"])
        changed = asyncio.Event()

        async def navigate_after_first_frame() -> None:
            while not self.frames(seen):
                await asyncio.sleep(0.05)
            await self.op("open", {"url": self.site.url("/dentro")})
            changed.set()

        seen: list[dict] = []

        def until(events: list[dict]) -> bool:
            seen[:] = events
            return changed.is_set() and len(self.frames(events)) >= 2

        mover = asyncio.create_task(navigate_after_first_frame())
        events = await self.events(until)
        await mover
        frames = self.frames(events)
        first = base64.b64decode(frames[0]["data"])
        self.assertEqual(first[:3], b"\xff\xd8\xff")  # JPEG
        self.assertGreater(frames[0]["width"], 0)
        self.assertNotEqual(frames[0]["data"], frames[-1]["data"])

    async def test_stops_when_nobody_watches(self) -> None:
        await self.op("open", {"url": self.site.url("/horarios")})
        await self.events(lambda seen: bool(self.frames(seen)))
        await asyncio.sleep(0.3)
        live = self.client.server.app[SCREENCAST]
        self.assertEqual(live.watching, 0)
        self.assertFalse(live.running)


class DeliveryTest(unittest.IsolatedAsyncioTestCase):
    """Reparto sin navegador: nadie se queda con fotogramas viejos y quien llega ve el último."""

    async def test_keeps_the_latest_and_never_falls_behind(self) -> None:
        live = Screencast(lambda: None)
        queue: asyncio.Queue = asyncio.Queue(maxsize=2)
        live._subscribers.add(queue)
        for n in range(3):
            live._publish({"type": "frame", "data": str(n)})
        # Quien va con retraso se queda con los más recientes.
        self.assertEqual([queue.get_nowait()["data"], queue.get_nowait()["data"]], ["1", "2"])
        live._subscribers.discard(queue)
        # Quien llega después recibe el último enseguida; al irse, la emisión se para.
        async with live.watch() as late:
            self.assertEqual(late.get_nowait()["data"], "2")
        self.assertFalse(live.running)

    async def test_waiting_with_a_deadline_does_not_end_the_subscription(self) -> None:
        live = Screencast(lambda: None)
        async with live.watch() as events:
            self.assertEqual((await events.get())["type"], "idle")
            with self.assertRaises(asyncio.TimeoutError):
                await asyncio.wait_for(events.get(), timeout=0.1)
            live._publish({"type": "frame", "data": "x"})
            self.assertEqual((await asyncio.wait_for(events.get(), timeout=1))["data"], "x")
            self.assertEqual(live.watching, 1)


if __name__ == "__main__":
    unittest.main()
