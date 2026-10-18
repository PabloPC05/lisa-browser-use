"""Acceso al baúl de contraseñas (Vaultwarden) y coincidencia de dominios.

Las contraseñas solo salen de aquí hacia el ejecutor, que las escribe en la
página. Nunca van en argumentos de programas ni en mensajes de error.
"""

from __future__ import annotations

import base64
import json
import os
import subprocess
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Protocol, Sequence
from urllib.parse import urlparse

import tldextract

# Lista de sufijos incluida en el paquete: sin descargas en tiempo de ejecución.
_extract = tldextract.TLDExtract(suffix_list_urls=(), cache_dir=None)


def registrable_domain(url_or_host: str) -> str:
    """`https://aulas.usc.gal/x` -> `usc.gal`. Para IPs o nombres sin sufijo público, el nombre entero."""
    host = urlparse(url_or_host).hostname if "://" in url_or_host else url_or_host.split("/")[0].split(":")[0]
    host = (host or "").lower().rstrip(".")
    parts = _extract(host)
    registered = ".".join(p for p in (parts.domain, parts.suffix) if p) if parts.suffix else ""
    return registered or host


# Dominios que comparten unas mismas credenciales. Cl@ve Permanente pide la contraseña en
# servidores de varios organismos (la pasarela en clave.gob.es, el formulario en la Seguridad
# Social y, según el trámite, la Agencia Tributaria): la entrada del baúl vale en todos ellos.
EQUIVALENT_DOMAINS: tuple[frozenset[str], ...] = (
    frozenset({"clave.gob.es", "seg-social.gob.es", "seg-social.es", "agenciatributaria.gob.es", "agenciatributaria.es"}),
)


def equivalent_domains(domain: str) -> frozenset[str]:
    """El grupo de dominios equivalentes al de `domain` (él mismo si no tiene)."""
    return next((group for group in EQUIVALENT_DOMAINS if domain in group), frozenset({domain}) if domain else frozenset())


@dataclass(frozen=True)
class Entry:
    id: str
    name: str
    username: str
    password: str = field(repr=False)
    uris: tuple[str, ...] = ()
    has_totp: bool = False

    def matches(self, page_url: str) -> bool:
        """Una contraseña solo vale para el dominio guardado junto a ella (o uno equivalente, como en Cl@ve)."""
        domains = equivalent_domains(registrable_domain(page_url))
        return bool(domains) and any(registrable_domain(uri) in domains for uri in self.uris)


class VaultUnavailable(RuntimeError):
    pass


class Vault(Protocol):
    def find(self, url: str) -> list[Entry]:
        """Entradas guardadas para el sitio de `url`."""

    def search(self, text: str) -> list[Entry]:
        """Entradas cuyo nombre o dirección contiene `text`."""

    def totp(self, entry: Entry) -> str:
        """Código temporal de la entrada, si tiene generador."""

    def set_password(self, entry: Entry, password: str) -> None:
        """Guarda `password` como contraseña de la entrada."""


class FakeVault:
    """Baúl en memoria para pruebas."""

    def __init__(
        self, entries: Sequence[Entry] = (), *, codes: dict[str, str] | None = None, broken: bool = False,
    ):
        self.entries = list(entries)
        self.codes = dict(codes or {})
        self.broken = broken
        self.totp_calls = 0

    def find(self, url: str) -> list[Entry]:
        if self.broken:
            raise VaultUnavailable("baúl de pruebas no disponible")
        return [e for e in self.entries if e.matches(url)]

    def search(self, text: str) -> list[Entry]:
        needle = text.lower()
        return [e for e in self.entries if needle in e.name.lower() or any(needle in u.lower() for u in e.uris)]

    def totp(self, entry: Entry) -> str:
        self.totp_calls += 1
        return self.codes[entry.id]

    def set_password(self, entry: Entry, password: str) -> None:
        if self.broken:
            raise VaultUnavailable("baúl de pruebas no disponible")
        self.entries = [replace(e, password=password) if e.id == entry.id else e for e in self.entries]


class BitwardenCliVault:
    """Baúl de Vaultwarden a través de `bw`, la línea de comandos de Bitwarden.

    Los secretos se pasan a `bw` por variables de entorno. La clave de sesión que
    devuelve al abrir el baúl queda solo en la memoria de este proceso.
    """

    def __init__(
        self, *, server: str, client_id: str, client_secret: str, master_password: str,
        data_dir: str | Path, bw: str = "bw", timeout_s: float = 60,
    ) -> None:
        self._server = server
        self._secrets = {"BW_CLIENTID": client_id, "BW_CLIENTSECRET": client_secret, "BW_PASSWORD": master_password}
        self._data_dir = str(data_dir)
        self._bw = bw
        self._timeout_s = timeout_s
        self._session: str | None = None

    def find(self, url: str) -> list[Entry]:
        # bw busca por el dominio de la dirección: en un grupo equivalente se busca en cada uno.
        found: dict[str, Entry] = {}
        domain = registrable_domain(url)
        for other in sorted(equivalent_domains(domain) - {domain}):
            found.update((e.id, e) for e in self._items("--url", f"https://{other}/"))
        found.update((e.id, e) for e in self._items("--url", url))
        return [e for e in found.values() if e.matches(url)]

    def search(self, text: str) -> list[Entry]:
        return self._items("--search", text)

    def totp(self, entry: Entry) -> str:
        return self._run("get", "totp", entry.id).strip()

    def set_password(self, entry: Entry, password: str) -> None:
        try:
            item = json.loads(self._run("get", "item", entry.id))
        except ValueError as exc:
            raise VaultUnavailable("bw devolvió una respuesta que no es JSON") from exc
        if item.get("type") != 1 or not isinstance(item.get("login"), dict):
            raise VaultUnavailable("la entrada del baúl no es un inicio de sesión")
        item["login"]["password"] = password
        # La entrada cifrada en base64 entra por la entrada estándar, no como argumento.
        self._run("edit", "item", entry.id, stdin=base64.b64encode(json.dumps(item).encode()).decode())

    def _items(self, flag: str, value: str) -> list[Entry]:
        try:
            raw = json.loads(self._run("list", "items", flag, value))
        except ValueError as exc:
            raise VaultUnavailable("bw devolvió una respuesta que no es JSON") from exc
        entries = []
        for item in raw:
            login = item.get("login") or {}
            if item.get("type") != 1 or not login.get("password"):
                continue
            entries.append(Entry(
                id=item["id"],
                name=item.get("name") or "",
                username=login.get("username") or "",
                password=login["password"],
                uris=tuple(u["uri"] for u in login.get("uris") or () if u.get("uri")),
                has_totp=bool(login.get("totp")),
            ))
        return entries

    def _run(self, *args: str, stdin: str | None = None) -> str:
        if self._session is None:
            self._open()
        return self._bw_call(*args, env={"BW_SESSION": self._session or ""}, stdin=stdin)

    def _open(self) -> None:
        status = json.loads(self._bw_call("status")).get("status")
        if status == "unauthenticated":
            self._bw_call("config", "server", self._server)
            self._bw_call("login", "--apikey", env=self._secrets)
        self._session = self._bw_call("unlock", "--passwordenv", "BW_PASSWORD", "--raw", env=self._secrets).strip()
        if not self._session:
            raise VaultUnavailable("bw no devolvió una sesión al abrir el baúl")
        self._bw_call("sync", env={"BW_SESSION": self._session})

    def _bw_call(self, *args: str, env: dict[str, str] | None = None, stdin: str | None = None) -> str:
        environment = {
            "PATH": os.environ.get("PATH", ""),
            "HOME": self._data_dir,
            "BITWARDENCLI_APPDATA_DIR": self._data_dir,
            **(env or {}),
        }
        try:
            done = subprocess.run(
                [self._bw, *args, "--nointeraction"], env=environment, capture_output=True, text=True,
                timeout=self._timeout_s, input=stdin,
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise VaultUnavailable(f"no se pudo ejecutar bw {args[0]} ({type(exc).__name__})") from exc
        if done.returncode != 0:
            # La salida de error de bw no se reproduce: podría contener datos del baúl.
            raise VaultUnavailable(f"bw {args[0]} terminó con código {done.returncode}")
        return done.stdout
