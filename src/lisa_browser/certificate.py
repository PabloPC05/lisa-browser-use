"""Certificado digital de Pablo (FNMT u otro `.p12`) para identificarse en sedes electrónicas.

Chromium no puede elegir un certificado de cliente sin pantalla: el diálogo nativo no tiene
dónde salir. Por eso el certificado lo presenta un proxy aparte (`sidecar/`, en Go, tomado de
SpainAssist): Chromium manda por él los dominios oficiales; el proxy termina el TLS con una
clave propia que Chromium acepta (pin SPKI) y habla con la sede con el certificado de Pablo.

La clave privada sale del baúl solo hacia la memoria de este proceso y del proxy, por la
entrada estándar. Nunca se escribe en disco, en el registro ni en un mensaje de error.
"""

from __future__ import annotations

import base64
import json
import logging
import subprocess
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Sequence

log = logging.getLogger(__name__)

# Tiempo que el certificado sigue activo tras el último inicio de sesión con él.
ACTIVE_SECONDS = 30 * 60


class CertificateError(RuntimeError):
    """El certificado no se pudo usar. `code` va al planificador; el texto nunca lleva secretos."""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code


@dataclass(frozen=True)
class Identity:
    """Certificado listo para el proxy: cadena (hoja primero) y clave en PKCS#8."""

    chain_der: tuple[bytes, ...]
    key_pkcs8: bytes = field(repr=False)
    holder: str
    issuer: str
    not_after: datetime

    def spec(self, ident: str) -> dict[str, Any]:
        return {
            "id": ident,
            "chain": [base64.b64encode(c).decode() for c in self.chain_der],
            "key": base64.b64encode(self.key_pkcs8).decode(),
        }

    def summary(self) -> dict[str, Any]:
        return {"holder": self.holder, "issuer": self.issuer, "expires": self.not_after.date().isoformat()}


def load_p12(data: bytes, password: str) -> Identity:
    """Abre un `.p12`/`.pfx`. `bad_password` si la contraseña no vale; `invalid` si no es un certificado."""
    from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, pkcs12
    from cryptography.x509.oid import NameOID

    try:
        key, cert, extra = pkcs12.load_key_and_certificates(data, password.encode() if password else None)
    except ValueError as exc:
        # cryptography no distingue contraseña mala de archivo dañado; casi siempre es la contraseña.
        raise CertificateError("bad_password", "no se pudo abrir el certificado con su contraseña") from exc
    if key is None or cert is None:
        raise CertificateError("invalid", "el archivo no contiene un certificado con su clave")

    def name(n: Any) -> str:
        for oid in (NameOID.COMMON_NAME, NameOID.ORGANIZATION_NAME):
            values = n.get_attributes_for_oid(oid)
            if values:
                return str(values[0].value)
        return n.rfc4514_string()

    return Identity(
        chain_der=(cert.public_bytes(Encoding.DER), *(c.public_bytes(Encoding.DER) for c in extra or ())),
        key_pkcs8=key.private_bytes(Encoding.DER, PrivateFormat.PKCS8, NoEncryption()),
        holder=name(cert.subject),
        issuer=name(cert.issuer),
        not_after=cert.not_valid_after_utc,
    )


def pem_to_pkcs8_der(pem: str) -> bytes:
    """Clave del proxy: PEM PKCS#8 (`openssl genpkey`) -> DER."""
    body = "".join(line for line in pem.splitlines() if line and not line.startswith("-----"))
    return base64.b64decode(body)


class Sidecar:
    """El proceso del proxy y su protocolo de líneas JSON (sidecar/protocol.go)."""

    def __init__(
        self, binary: str, *, proxy_key_pem: str, listen: str, allowed_peers: Sequence[str],
        allowed_hosts: Sequence[str], roots: Sequence[str] = (), allow_private: Sequence[str] = (),
        spawn: Callable[..., Any] = subprocess.Popen,
    ) -> None:
        self._binary = binary
        self._proxy_key = pem_to_pkcs8_der(proxy_key_pem)
        self.listen = listen
        self._peers = list(allowed_peers)
        self._hosts = list(allowed_hosts)
        # Normalmente vacío: el proxy verifica las sedes con el almacén de confianza del sistema.
        # Solo las pruebas pasan aquí la CA de su servidor de mentira.
        self._roots = list(roots)
        # Solo pruebas: direcciones internas que el proxy puede alcanzar.
        self._allow_private = list(allow_private)
        self._spawn = spawn
        self._proc: Any = None
        self._lock = threading.Lock()
        self._acks: dict[int, Any] = {}
        self._ack_ready = threading.Condition()
        self._next_id = 0
        self.spki = ""
        # Puerto real donde escucha el proxy (lo dice el primer ack; útil si se pidió el 0).
        self.port = 0
        self.events: list[dict[str, Any]] = []

    def start(self) -> None:
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return
            self._proc = self._spawn(
                [self._binary], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, bufsize=0,
            )
            config: dict[str, Any] = {
                "type": "config", "key": base64.b64encode(self._proxy_key).decode(),
                "allowedPeers": self._peers, "allowedHosts": self._hosts,
            }
            if self._roots:
                config["roots"] = self._roots
            if self._allow_private:
                config["allowPrivateHosts"] = self._allow_private
            self._write(config)
            ready = json.loads(self._proc.stdout.readline() or b"{}")
            if ready.get("type") != "ready":
                err = self._proc.stderr.read().decode(errors="replace")[:300] if self._proc.poll() is not None else ""
                raise CertificateError("proxy_unavailable", f"el proxy de certificados no arrancó {err}".strip())
            self.spki = ready.get("spki", "")
            threading.Thread(target=self._read, name="sidecar", daemon=True).start()
            # El puerto se abre con el primer mensaje de la sesión, sin certificado todavía.
            self._configure_locked([], None)
            log.info("proxy de certificados en %s (SPKI %s)", self.listen, self.spki)

    def configure(self, hosts: Sequence[str], identity: dict[str, Any] | None) -> None:
        self.start()
        with self._lock:
            self._configure_locked(hosts, identity)

    def _configure_locked(self, hosts: Sequence[str], identity: dict[str, Any] | None) -> None:
        self._next_id += 1
        ident = self._next_id
        message: dict[str, Any] = {"type": "session", "id": ident, "session": "lisa", "listen": self.listen, "hosts": list(hosts)}
        if identity is not None:
            message["identity"] = identity
        self._write(message)
        ack = self._wait_ack(ident)
        if ack.get("error"):
            raise CertificateError("proxy_error", str(ack["error"])[:200])
        if ack.get("port"):
            self.port = int(ack["port"])

    def _write(self, message: dict[str, Any]) -> None:
        self._proc.stdin.write(json.dumps(message).encode() + b"\n")
        self._proc.stdin.flush()

    def _wait_ack(self, ident: int, timeout: float = 10.0) -> dict[str, Any]:
        deadline = time.monotonic() + timeout
        with self._ack_ready:
            while ident not in self._acks:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise CertificateError("proxy_unavailable", "el proxy de certificados no respondió")
                self._ack_ready.wait(left)
            return self._acks.pop(ident)

    def _read(self) -> None:
        proc = self._proc
        for line in iter(proc.stdout.readline, b""):
            try:
                message = json.loads(line)
            except ValueError:
                continue
            kind = message.get("type")
            if kind == "ack":
                with self._ack_ready:
                    self._acks[int(message.get("id", 0))] = message
                    self._ack_ready.notify_all()
            elif kind == "event":
                self.events = (self.events + [message])[-50:]
                log.info("proxy: %s %s %s", message.get("event"), message.get("host"), message.get("code") or "")
            elif kind == "log":
                log.info("proxy: %s", message.get("message"))
            elif kind == "call":
                # Solo las claves remotas (DNIe) firman por llamadas; el .p12 firma dentro del proxy.
                self._write({"type": "reply", "id": message.get("id"), "error": "unsupported"})

    def close(self) -> None:
        with self._lock:
            if self._proc is not None:
                try:
                    self._proc.stdin.close()
                    self._proc.wait(timeout=5)
                except Exception:
                    self._proc.kill()
                for pipe in (self._proc.stdout, self._proc.stderr):
                    if pipe is not None:
                        pipe.close()
                self._proc = None


@dataclass(frozen=True)
class StoredCertificate:
    """Un certificado del baúl: la entrada, el adjunto y la contraseña con que se abre."""

    item_id: str
    name: str
    attachment_id: str
    file_name: str
    password: str = field(repr=False)


class Certificates:
    """Activa el certificado del baúl en el proxy, solo para los dominios oficiales configurados."""

    def __init__(
        self, sidecar: Sidecar, *, hosts: Sequence[str], active_seconds: float = ACTIVE_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._sidecar = sidecar
        self.hosts = tuple(hosts)
        self._active_seconds = active_seconds
        self._clock = clock
        self._until = 0.0
        self._timer: threading.Timer | None = None
        self.active: Identity | None = None

    def login(self, vault: Any, account: str = "") -> dict[str, Any]:
        """Devuelve `{status, ...}`: `ok`, `no_certificate`, `choose_certificate`, `bad_password`, `invalid`,
        `expired`, `vault_unavailable` o `proxy_unavailable`."""
        from .vault import VaultUnavailable

        try:
            stored = vault.certificates()
        except VaultUnavailable as exc:
            return {"status": "vault_unavailable", "reason": str(exc)}
        wanted = account.strip().lower()
        if wanted:
            stored = [c for c in stored if wanted in c.name.lower() or wanted in c.file_name.lower()] or stored
        if not stored:
            return {"status": "no_certificate", "reason": "el baúl no tiene ningún adjunto .p12 o .pfx"}
        if len(stored) > 1:
            return {"status": "choose_certificate", "certificates": [{"name": c.name, "file": c.file_name} for c in stored]}
        chosen = stored[0]
        try:
            data = vault.attachment(chosen.item_id, chosen.attachment_id)
            identity = load_p12(data, chosen.password)
        except VaultUnavailable as exc:
            return {"status": "vault_unavailable", "reason": str(exc)}
        except CertificateError as exc:
            return {"status": exc.code, "reason": str(exc)}
        if identity.not_after <= datetime.now(timezone.utc):
            return {"status": "expired", **identity.summary()}
        try:
            self._sidecar.configure(self.hosts, identity.spec(chosen.item_id))
        except CertificateError as exc:
            return {"status": exc.code, "reason": str(exc)}
        self.active = identity
        self._renew()
        return {"status": "ok", "method": "certificate", **identity.summary(), "minutes": int(self._active_seconds // 60)}

    def _renew(self) -> None:
        if self._timer is not None:
            self._timer.cancel()
        self._timer = threading.Timer(self._active_seconds, self.deactivate)
        self._timer.daemon = True
        self._timer.start()

    def deactivate(self) -> None:
        """Quita el certificado del proxy: las sedes vuelven a verse sin él."""
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if self.active is None:
            return
        self.active = None
        try:
            self._sidecar.configure([], None)
        except CertificateError:
            log.warning("no se pudo quitar el certificado del proxy; se cierra el proxy")
            self._sidecar.close()


def is_certificate_file(name: str) -> bool:
    return Path(name.lower()).suffix in (".p12", ".pfx")
