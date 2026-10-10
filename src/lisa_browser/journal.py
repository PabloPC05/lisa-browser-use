"""Registro de lo que Lisa hace en el navegador (SPEC R12, PLAN §4.4).

Guarda qué se pidió, por dónde se pasó, qué acciones con efecto hubo y con
qué aprobación, y cuánto costó. Nunca guarda contraseñas, códigos, valores de
`inputs` ni imágenes: todo texto pasa por la redacción de secretos.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from arc_cua.safety import redact

_SCHEMA = """
CREATE TABLE IF NOT EXISTS operations (
  id TEXT PRIMARY KEY,
  conversation TEXT,
  op TEXT NOT NULL,
  goal TEXT,
  started_at TEXT NOT NULL,
  seconds REAL,
  status TEXT,
  steps INTEGER NOT NULL DEFAULT 0,
  input_tokens INTEGER NOT NULL DEFAULT 0,
  cost_usd REAL NOT NULL DEFAULT 0,
  sites TEXT
);
CREATE TABLE IF NOT EXISTS steps (
  operation_id TEXT NOT NULL REFERENCES operations(id),
  n INTEGER NOT NULL,
  action TEXT NOT NULL,
  role TEXT,
  name TEXT,
  ms INTEGER,
  decided_by TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS effects (
  operation_id TEXT NOT NULL REFERENCES operations(id),
  at TEXT NOT NULL,
  category TEXT NOT NULL,
  domain TEXT NOT NULL,
  action TEXT NOT NULL,
  decision TEXT NOT NULL
);
"""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Journal:
    def __init__(self, path: str | Path) -> None:
        # El servidor atiende desde varios hilos; las escrituras son cortas.
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()
        self._secrets: dict[str, tuple[str, ...]] = {}

    def close(self) -> None:
        self._db.close()

    def start(self, conversation: str | None, op: str, goal: str | None, secrets: Iterable[str] = ()) -> str:
        """Abre una operación. `secrets` son los valores que no deben quedar escritos."""
        operation_id = "o_" + uuid.uuid4().hex
        self._secrets[operation_id] = tuple(sorted((s for s in secrets if s), key=len, reverse=True))
        with self._db:
            self._db.execute(
                "INSERT INTO operations (id, conversation, op, goal, started_at) VALUES (?, ?, ?, ?, ?)",
                (operation_id, conversation, op, self._clean(operation_id, goal), _now()),
            )
        return operation_id

    def step(
        self, operation_id: str, n: int, action: str, role: str | None, name: str | None,
        ms: int | None, decided_by: str,
    ) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO steps VALUES (?, ?, ?, ?, ?, ?, ?)",
                (operation_id, n, action, role, self._clean(operation_id, name), ms, decided_by),
            )

    def effect(self, operation_id: str, category: str, domain: str, action: str, decision: str) -> None:
        with self._db:
            self._db.execute(
                "INSERT INTO effects VALUES (?, ?, ?, ?, ?, ?)",
                (operation_id, _now(), category, domain, self._clean(operation_id, action), decision),
            )

    def finish(
        self, operation_id: str, status: str, seconds: float, input_tokens: int = 0,
        cost_usd: float = 0.0, sites: Iterable[str] = (),
    ) -> None:
        with self._db:
            self._db.execute(
                "UPDATE operations SET status=?, seconds=?, input_tokens=?, cost_usd=?, sites=?,"
                " steps=(SELECT COUNT(*) FROM steps WHERE operation_id=?) WHERE id=?",
                (status, seconds, input_tokens, cost_usd, json.dumps(sorted(set(sites))), operation_id, operation_id),
            )
        self._secrets.pop(operation_id, None)

    def summary(self, since: str | None = None) -> list[dict]:
        """Operaciones desde `since` (fecha ISO), de la más antigua a la más reciente."""
        rows = self._db.execute(
            "SELECT id, conversation, op, goal, started_at, seconds, status, steps, input_tokens, cost_usd, sites"
            " FROM operations WHERE started_at >= ? ORDER BY started_at, rowid",
            (since or "",),
        ).fetchall()
        out = []
        for row in rows:
            effects = self._db.execute(
                "SELECT at, category, domain, action, decision FROM effects WHERE operation_id=? ORDER BY rowid",
                (row[0],),
            ).fetchall()
            decided = self._db.execute(
                "SELECT decided_by, COUNT(*), AVG(ms) FROM steps WHERE operation_id=? GROUP BY decided_by", (row[0],)
            ).fetchall()
            out.append({
                "id": row[0], "conversation": row[1], "op": row[2], "goal": row[3], "started_at": row[4],
                "seconds": row[5], "status": row[6], "steps": row[7], "input_tokens": row[8],
                "cost_usd": row[9], "sites": json.loads(row[10]) if row[10] else [],
                "decided_by": {who: {"steps": count, "avg_ms": round(avg) if avg is not None else None}
                               for who, count, avg in decided},
                "effects": [dict(zip(("at", "category", "domain", "action", "decision"), e)) for e in effects],
            })
        return out

    def _clean(self, operation_id: str, text: str | None) -> str | None:
        return redact(text, self._secrets.get(operation_id, ())) if text else text
