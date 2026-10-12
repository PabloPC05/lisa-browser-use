"""Biblioteca: los sitios habituales de Pablo y las rutinas que Lisa ya sabe hacer.

Sin ella, cada gestión se redescubre: el planificador (Claude) tantea direcciones y
decide paso a paso, y cada una de esas decisiones es un turno entero del modelo caro.
Con ella:

- un **sitio** guarda la dirección de entrada y dónde está cada cosa, para ir directo;
- una **rutina** guarda los pasos que ya funcionaron para una gestión repetida. El
  ejecutor la hace entera, con JEV decidiendo cada acción, sin que el planificador
  intervenga entre paso y paso.

Aquí no se guardan contraseñas ni códigos: el inicio de sesión de una rutina sigue
saliendo del baúl, y las acciones con efecto siguen pasando por la barrera.
"""

from __future__ import annotations

import json
import re
import sqlite3
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

from .vault import registrable_domain

# Lo que puede hacer un paso de una rutina. Adjuntar y cerrar sesión no se automatizan.
ROUTINE_OPS = ("open", "follow", "task", "login", "read")
MAX_STEPS = 20
MAX_NOTES = 600

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sites (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  url TEXT NOT NULL,
  domain TEXT NOT NULL,
  notes TEXT NOT NULL DEFAULT '',
  aliases TEXT NOT NULL DEFAULT '[]',
  updated_at TEXT NOT NULL,
  uses INTEGER NOT NULL DEFAULT 0,
  last_used TEXT
);
CREATE TABLE IF NOT EXISTS routines (
  key TEXT PRIMARY KEY,
  name TEXT NOT NULL,
  description TEXT NOT NULL,
  params TEXT NOT NULL DEFAULT '[]',
  steps TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  runs INTEGER NOT NULL DEFAULT 0,
  ok INTEGER NOT NULL DEFAULT 0,
  last_run TEXT,
  last_status TEXT
);
"""

_PARAM = re.compile(r"\{([a-z_][a-z0-9_]*)\}")
_STEP_KEYS = {
    "open": {"url": str, "find": str, "read_chars": int},
    "follow": {"text": str, "find": str, "read_chars": int},
    "task": {"goal": str, "verification": list, "inputs": dict, "constraints": list, "max_actions": int, "find": str,
             "read_chars": int},
    "login": {"account": str},
    "read": {"find": str, "max_chars": int},
}


class LibraryError(ValueError):
    """Algo que no se puede guardar o ejecutar. El mensaje va al planificador."""


class MissingParams(LibraryError):
    def __init__(self, missing: Iterable[str]) -> None:
        self.missing = sorted(missing)
        super().__init__("faltan datos para la rutina: " + ", ".join(self.missing))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def key_of(name: str) -> str:
    """Los nombres se comparan sin mayúsculas, tildes ni espacios de más."""
    plain = unicodedata.normalize("NFD", " ".join(str(name).split()).casefold())
    return "".join(c for c in plain if not unicodedata.combining(c))


def _name(name: Any) -> str:
    clean = " ".join(str(name or "").split())
    if not 1 <= len(clean) <= 60 or any(c in clean for c in "\n\r\t|"):
        raise LibraryError("el nombre debe tener entre 1 y 60 caracteres, en una línea")
    return clean


def _strings(value: Any) -> Iterable[str]:
    if isinstance(value, str):
        yield value
    elif isinstance(value, Mapping):
        for item in value.values():
            yield from _strings(item)
    elif isinstance(value, (list, tuple)):
        for item in value:
            yield from _strings(item)


def check_steps(steps: Any) -> list[dict[str, Any]]:
    """Valida los pasos de una rutina y los devuelve solo con los campos conocidos."""
    if not isinstance(steps, list) or not 1 <= len(steps) <= MAX_STEPS:
        raise LibraryError(f"una rutina tiene entre 1 y {MAX_STEPS} pasos")
    clean: list[dict[str, Any]] = []
    for n, step in enumerate(steps, 1):
        if not isinstance(step, Mapping) or step.get("op") not in ROUTINE_OPS:
            raise LibraryError(f"paso {n}: `op` debe ser uno de {', '.join(ROUTINE_OPS)}")
        op = step["op"]
        out: dict[str, Any] = {"op": op}
        for field, kind in _STEP_KEYS[op].items():
            value = step.get(field)
            if value in (None, "", [], {}):
                continue
            if not isinstance(value, kind) or isinstance(value, bool):
                raise LibraryError(f"paso {n}: `{field}` no tiene el tipo esperado")
            out[field] = value
        if op == "open" and not re.match(r"^(https?://|\{)", out.get("url", "")):
            raise LibraryError(f"paso {n}: `open` necesita una dirección http(s) en `url`")
        if op == "follow" and "text" not in out:
            raise LibraryError(f"paso {n}: `follow` necesita en `text` el texto del enlace")
        if op == "task":
            if "goal" not in out or not out.get("verification"):
                raise LibraryError(f"paso {n}: `task` necesita `goal` y `verification`")
            if not all(isinstance(v, str) for v in out["verification"]):
                raise LibraryError(f"paso {n}: `verification` es una lista de frases")
            if not all(isinstance(k, str) and isinstance(v, str) for k, v in out.get("inputs", {}).items()):
                raise LibraryError(f"paso {n}: `inputs` asocia nombres a textos")
        clean.append(out)
    return clean


def params_of(steps: Any) -> list[str]:
    """Los datos que hay que darle a la rutina: los `{nombre}` que aparecen en sus pasos."""
    return sorted({match for text in _strings(steps) for match in _PARAM.findall(text)})


def substitute(steps: list[dict[str, Any]], params: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Pone los datos de esta ejecución en los `{nombre}` de los pasos."""
    given = {str(k): str(v) for k, v in (params or {}).items() if v not in (None, "")}
    missing = set(params_of(steps)) - set(given)
    if missing:
        raise MissingParams(missing)

    def fill(value: Any) -> Any:
        if isinstance(value, str):
            return _PARAM.sub(lambda m: given[m.group(1)], value)
        if isinstance(value, dict):
            return {k: fill(v) for k, v in value.items()}
        if isinstance(value, list):
            return [fill(v) for v in value]
        return value

    return [fill(step) for step in steps]


class Library:
    def __init__(self, path: str | Path) -> None:
        # El servidor atiende desde varios hilos; las escrituras son cortas.
        self._db = sqlite3.connect(str(path), check_same_thread=False)
        self._db.execute("PRAGMA journal_mode = WAL")
        self._db.executescript(_SCHEMA)
        self._db.commit()

    def close(self) -> None:
        self._db.close()

    # --- sitios ---------------------------------------------------------------------

    def save_site(self, name: Any, url: Any, notes: Any = "", aliases: Any = ()) -> dict[str, Any]:
        name = _name(name)
        url = str(url or "").strip()
        if not re.match(r"^https?://\S+$", url):
            raise LibraryError("la dirección del sitio debe empezar por http:// o https://")
        notes = " ".join(str(notes or "").split())
        if len(notes) > MAX_NOTES:
            raise LibraryError(f"las notas de un sitio caben en {MAX_NOTES} caracteres: di solo dónde está cada cosa")
        names = [_name(a) for a in (aliases or ()) if str(a).strip()]
        with self._db:
            self._db.execute(
                "INSERT INTO sites (key, name, url, domain, notes, aliases, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET name=excluded.name, url=excluded.url, domain=excluded.domain,"
                " notes=excluded.notes, aliases=excluded.aliases, updated_at=excluded.updated_at",
                (key_of(name), name, url, registrable_domain(url), notes, json.dumps(names, ensure_ascii=False), _now()),
            )
        return {"name": name, "url": url, "notes": notes, "aliases": names}

    def delete_site(self, name: Any) -> bool:
        with self._db:
            return self._db.execute("DELETE FROM sites WHERE key=?", (key_of(str(name)),)).rowcount > 0

    def sites(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT name, url, notes, aliases, uses FROM sites ORDER BY uses DESC, name COLLATE NOCASE"
        ).fetchall()
        return [{"name": n, "url": u, "notes": notes, "aliases": json.loads(a), "uses": uses} for n, u, notes, a, uses in rows]

    def visited(self, url: str) -> None:
        """Apunta que se ha pasado por un sitio guardado, para ordenar los más usados primero."""
        domain = registrable_domain(url)
        if domain:
            with self._db:
                self._db.execute("UPDATE sites SET uses=uses+1, last_used=? WHERE domain=?", (_now(), domain))

    # --- rutinas --------------------------------------------------------------------

    def save_routine(self, name: Any, description: Any, steps: Any) -> dict[str, Any]:
        name = _name(name)
        description = " ".join(str(description or "").split())
        if not 1 <= len(description) <= 300:
            raise LibraryError("la descripción dice en una frase (hasta 300 caracteres) qué consigue la rutina")
        clean = check_steps(steps)
        params = params_of(clean)
        with self._db:
            self._db.execute(
                "INSERT INTO routines (key, name, description, params, steps, updated_at) VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(key) DO UPDATE SET name=excluded.name, description=excluded.description,"
                " params=excluded.params, steps=excluded.steps, updated_at=excluded.updated_at",
                (key_of(name), name, description, json.dumps(params), json.dumps(clean, ensure_ascii=False), _now()),
            )
        return {"name": name, "description": description, "params": params, "steps": len(clean)}

    def delete_routine(self, name: Any) -> bool:
        with self._db:
            return self._db.execute("DELETE FROM routines WHERE key=?", (key_of(str(name)),)).rowcount > 0

    def routine(self, name: Any) -> dict[str, Any] | None:
        row = self._db.execute(
            "SELECT name, description, params, steps, runs, ok, last_run, last_status FROM routines WHERE key=?",
            (key_of(str(name)),),
        ).fetchone()
        if row is None:
            return None
        return {
            "name": row[0], "description": row[1], "params": json.loads(row[2]), "steps": json.loads(row[3]),
            "runs": row[4], "ok": row[5], "last_run": row[6], "last_status": row[7],
        }

    def routines(self) -> list[dict[str, Any]]:
        rows = self._db.execute(
            "SELECT name, description, params, steps, runs, ok, last_status FROM routines"
            " ORDER BY runs DESC, name COLLATE NOCASE"
        ).fetchall()
        return [
            {"name": n, "description": d, "params": json.loads(p), "steps": len(json.loads(s)), "runs": runs, "ok": ok,
             "last_status": last}
            for n, d, p, s, runs, ok, last in rows
        ]

    def ran(self, name: Any, ok: bool, status: str) -> None:
        with self._db:
            self._db.execute(
                "UPDATE routines SET runs=runs+1, ok=ok+?, last_run=?, last_status=? WHERE key=?",
                (1 if ok else 0, _now(), status, key_of(str(name))),
            )

    def listing(self) -> dict[str, Any]:
        """Lo que el planificador ve en cada mensaje: sitios y rutinas, sin los pasos."""
        return {"sites": self.sites(), "routines": self.routines()}
