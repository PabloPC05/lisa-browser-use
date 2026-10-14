"""Certificado digital del baúl y proxy de certificados (sin sedes reales ni datos reales)."""

from __future__ import annotations

import base64
import datetime as dt
import os
import unittest

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import pkcs12
from cryptography.x509.oid import NameOID

from lisa_browser.certificate import CertificateError, Certificates, Sidecar, load_p12
from lisa_browser.vault import FakeVault, VaultUnavailable

P12_PASSWORD = "clave-de-prueba"


def make_p12(*, cn: str = "NOMBRE APELLIDO PRUEBA - 00000000T", days: int = 365, password: str = P12_PASSWORD) -> bytes:
    """Certificado ficticio con su cadena, como los que exporta la FNMT."""
    now = dt.datetime.now(dt.timezone.utc)
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "AC Pruebas")])
    ca = (
        x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name).public_key(ca_key.public_key())
        .serial_number(1).not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=3650))
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True).sign(ca_key, hashes.SHA256())
    )
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    cert = (
        x509.CertificateBuilder().subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)]))
        .issuer_name(ca_name).public_key(key.public_key()).serial_number(2)
        .not_valid_before(now - dt.timedelta(days=400)).not_valid_after(now + dt.timedelta(days=days))
        .sign(ca_key, hashes.SHA256())
    )
    return pkcs12.serialize_key_and_certificates(
        b"cert", key, cert, [ca], serialization.BestAvailableEncryption(password.encode()),
    )


def item(item_id: str, name: str, file_name: str, *, password: str = P12_PASSWORD, as_field: bool = False) -> dict:
    return {
        "id": item_id, "name": name, "type": 1,
        "login": {"username": "", "password": "" if as_field else password},
        "fields": [{"name": "Contraseña", "value": password, "type": 1}] if as_field else [],
        "attachments": [{"id": "a-" + item_id, "fileName": file_name, "size": "1"}],
    }


class FakeSidecar:
    def __init__(self, broken: bool = False) -> None:
        self.calls: list[tuple[list, dict | None]] = []
        self.broken = broken

    def configure(self, hosts, identity) -> None:
        if self.broken:
            raise CertificateError("proxy_unavailable", "sin proxy")
        self.calls.append((list(hosts), identity))

    def close(self) -> None:
        pass


class LoadP12(unittest.TestCase):
    def test_opens_the_certificate_with_its_chain(self) -> None:
        identity = load_p12(make_p12(), P12_PASSWORD)
        self.assertEqual(identity.holder, "NOMBRE APELLIDO PRUEBA - 00000000T")
        self.assertEqual(identity.issuer, "AC Pruebas")
        self.assertEqual(len(identity.chain_der), 2)
        serialization.load_der_private_key(identity.key_pkcs8, None)
        self.assertNotIn(base64.b64encode(identity.key_pkcs8).decode()[:40], repr(identity))

    def test_wrong_password_says_so_without_the_password(self) -> None:
        with self.assertRaises(CertificateError) as caught:
            load_p12(make_p12(), "otra")
        self.assertEqual(caught.exception.code, "bad_password")
        self.assertNotIn("otra", str(caught.exception))


class CertificateLogin(unittest.TestCase):
    hosts = (".registradores.org", ".clave.gob.es")

    def login(self, vault: FakeVault, sidecar: FakeSidecar | None = None, account: str = "") -> dict:
        self.sidecar = sidecar or FakeSidecar()
        self.certificates = Certificates(self.sidecar, hosts=self.hosts, active_seconds=60)
        self.addCleanup(self.certificates.deactivate)
        return self.certificates.login(vault, account)

    def test_activates_the_certificate_only_for_the_official_domains(self) -> None:
        vault = FakeVault(items=[item("c1", "Certificado FNMT", "certificado.p12")], files={"a-c1": make_p12()})
        result = self.login(vault)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["holder"], "NOMBRE APELLIDO PRUEBA - 00000000T")
        [(hosts, identity)] = self.sidecar.calls
        self.assertEqual(hosts, list(self.hosts))
        self.assertEqual(identity["id"], "c1")
        # Al planificador no le llega nada de la clave.
        self.assertNotIn("key", result)
        self.assertNotIn(P12_PASSWORD, str(result))

    def test_password_from_a_custom_field_and_pfx_files(self) -> None:
        vault = FakeVault(items=[item("c1", "Cert", "mio.PFX", as_field=True)], files={"a-c1": make_p12()})
        self.assertEqual(self.login(vault)["status"], "ok")

    def test_deactivating_removes_it_from_the_proxy(self) -> None:
        vault = FakeVault(items=[item("c1", "Cert", "c.p12")], files={"a-c1": make_p12()})
        self.login(vault)
        self.certificates.deactivate()
        self.assertEqual(self.sidecar.calls[-1], ([], None))
        self.assertIsNone(self.certificates.active)

    def test_explains_every_way_it_can_fail(self) -> None:
        none = FakeVault(items=[{"id": "x", "name": "Otra", "type": 1, "login": {}, "attachments": [{"id": "1", "fileName": "foto.jpg"}]}])
        self.assertEqual(self.login(none)["status"], "no_certificate")

        two = FakeVault(items=[item("c1", "FNMT personal", "a.p12"), item("c2", "Representante", "b.p12")],
                        files={"a-c1": make_p12(), "a-c2": make_p12()})
        result = self.login(two)
        self.assertEqual(result["status"], "choose_certificate")
        self.assertEqual([c["name"] for c in result["certificates"]], ["FNMT personal", "Representante"])
        self.assertEqual(self.login(two, account="representante")["status"], "ok")
        self.assertEqual(self.sidecar.calls[0][1]["id"], "c2")

        bad = FakeVault(items=[item("c1", "Cert", "c.p12", password="mala")], files={"a-c1": make_p12()})
        self.assertEqual(self.login(bad)["status"], "bad_password")

        old = FakeVault(items=[item("c1", "Cert", "c.p12")], files={"a-c1": make_p12(days=-1)})
        self.assertEqual(self.login(old)["status"], "expired")

        broken = FakeVault(broken=True)
        self.assertEqual(self.login(broken)["status"], "vault_unavailable")

        fine = FakeVault(items=[item("c1", "Cert", "c.p12")], files={"a-c1": make_p12()})
        self.assertEqual(self.login(fine, FakeSidecar(broken=True))["status"], "proxy_unavailable")
        self.assertIsNone(self.certificates.active)


@unittest.skipUnless(os.environ.get("LISA_SIDECAR"), "sin el binario del proxy (LISA_SIDECAR)")
class RealSidecar(unittest.TestCase):
    """El protocolo con el proxy de verdad: arranca, abre su puerto y acepta un certificado."""

    def test_starts_listens_and_takes_a_certificate(self) -> None:
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption()).decode()
        sidecar = Sidecar(
            os.environ["LISA_SIDECAR"], proxy_key_pem=pem, listen="127.0.0.1:0",
            allowed_peers=["127.0.0.1"], allowed_hosts=["registradores.org"],
        )
        self.addCleanup(sidecar.close)
        sidecar.start()
        spki = key.public_key().public_bytes(serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo)
        from hashlib import sha256
        self.assertEqual(sidecar.spki, base64.b64encode(sha256(spki).digest()).decode())
        identity = load_p12(make_p12(), P12_PASSWORD)
        sidecar.configure([".registradores.org"], identity.spec("c1"))
        sidecar.configure([], None)
        with self.assertRaises(CertificateError):
            sidecar.configure(["mal/host"], identity.spec("c1"))


class VaultAttachments(unittest.TestCase):
    def test_broken_vault_raises(self) -> None:
        with self.assertRaises(VaultUnavailable):
            FakeVault(broken=True).certificates()


if __name__ == "__main__":
    unittest.main()
