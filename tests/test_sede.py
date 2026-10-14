"""Sede simulada que exige certificado de cliente, alcanzada por el proxy real (LISA-75).

Comprueba la cadena entera en Python: un servidor HTTPS que pide un certificado de cliente,
el binario del proxy (`sidecar/`) configurado como lo hace el ejecutor, y un cliente que pasa
por el proxy. Sin sedes reales ni datos reales: la CA, el certificado del servidor y el `.p12`
del cliente se generan en cada ejecución.
"""

from __future__ import annotations

import base64
import datetime as dt
import http.server
import ipaddress
import os
import ssl
import tempfile
import threading
import unittest
from pathlib import Path

import httpx
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from cryptography.hazmat.primitives.serialization import Encoding, NoEncryption, PrivateFormat, pkcs12
from cryptography.x509.oid import NameOID

from lisa_browser.certificate import Sidecar, load_p12


def _name(cn: str) -> x509.Name:
    return x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, cn)])


def _cert(subject: str, key, issuer_key, issuer_name: x509.Name, *, ca: bool = False, sans=()):
    now = dt.datetime.now(dt.timezone.utc)
    builder = (
        x509.CertificateBuilder().subject_name(_name(subject)).issuer_name(issuer_name)
        .public_key(key.public_key()).serial_number(x509.random_serial_number())
        .not_valid_before(now - dt.timedelta(days=1)).not_valid_after(now + dt.timedelta(days=2))
    )
    if ca:
        builder = builder.add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
    if sans:
        general = []
        for name in sans:
            try:
                general.append(x509.IPAddress(ipaddress.ip_address(name)))
            except ValueError:
                general.append(x509.DNSName(name))
        builder = builder.add_extension(x509.SubjectAlternativeName(general), critical=False)
    return builder.sign(issuer_key, hashes.SHA256())


class SimulatedSede:
    """Servidor HTTPS en 127.0.0.1 que pide (sin exigir) el certificado de cliente y dice quién lo presentó."""

    def __init__(self) -> None:
        self.ca_key = ec.generate_private_key(ec.SECP256R1())
        self._ca_name = _name("CA sede de pruebas")
        self.ca = _cert("CA sede de pruebas", self.ca_key, self.ca_key, self._ca_name, ca=True)
        srv_key = ec.generate_private_key(ec.SECP256R1())
        srv_cert = _cert("127.0.0.1", srv_key, self.ca_key, self._ca_name, sans=["127.0.0.1", "localhost"])
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        (tmp / "srv.pem").write_bytes(
            srv_cert.public_bytes(Encoding.PEM) + srv_key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()))
        (tmp / "ca.pem").write_bytes(self.ca.public_bytes(Encoding.PEM))
        self.ca_pem = self.ca.public_bytes(Encoding.PEM).decode()

        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(tmp / "srv.pem")
        ctx.load_verify_locations(tmp / "ca.pem")
        ctx.verify_mode = ssl.CERT_OPTIONAL  # pide el certificado, pero deja ver la diferencia con y sin él

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                cert = self.connection.getpeercert()
                if not cert:
                    self.send_response(403); self.end_headers(); self.wfile.write(b"Falta el certificado"); return
                cn = dict(x[0] for x in cert["subject"]).get("commonName", "")
                self.send_response(200); self.end_headers(); self.wfile.write(f"Hola, {cn}".encode())

            def log_message(self, *a) -> None:
                pass

        self._server = http.server.HTTPServer(("127.0.0.1", 0), Handler)
        self._server.socket = ctx.wrap_socket(self._server.socket, server_side=True)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)

    def client_p12(self, cn: str, password: str = "x") -> bytes:
        key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cert = _cert(cn, key, self.ca_key, self._ca_name)
        return pkcs12.serialize_key_and_certificates(
            b"c", key, cert, None, serialization.BestAvailableEncryption(password.encode()))

    def __enter__(self) -> "SimulatedSede":
        self._thread.start(); return self

    def __exit__(self, *_) -> None:
        self._server.shutdown(); self._server.server_close(); self._tmp.cleanup()


@unittest.skipUnless(os.environ.get("LISA_SIDECAR"), "sin el binario del proxy (LISA_SIDECAR)")
class CertificateReachesTheSede(unittest.TestCase):
    def setUp(self) -> None:
        self.sede = SimulatedSede().__enter__()
        self.addCleanup(self.sede.__exit__)
        key = ec.generate_private_key(ec.SECP256R1())
        pem = key.private_bytes(Encoding.PEM, PrivateFormat.PKCS8, NoEncryption()).decode()
        self.sidecar = Sidecar(
            os.environ["LISA_SIDECAR"], proxy_key_pem=pem, listen="127.0.0.1:0",
            allowed_peers=["127.0.0.1"], allowed_hosts=["127.0.0.1"], roots=[self.sede.ca_pem],
            allow_private=[f"127.0.0.1:{self.sede.port}"],
        )
        self.sidecar.start()
        self.addCleanup(self.sidecar.close)
        self.target = f"127.0.0.1:{self.sede.port}"
        self.host = f"127.0.0.1:{self.sidecar.port}"  # el puerto real que abrió el proxy

    def get(self) -> httpx.Response:
        # verify=False: la conexión con el proxy usa su clave efímera (en el navegador, el pin SPKI).
        with httpx.Client(proxy=f"http://{self.host}", verify=False, timeout=15) as client:
            return client.get(f"https://{self.target}/")

    def test_without_the_certificate_the_sede_refuses(self) -> None:
        self.assertEqual(self.get().status_code, 403)

    def test_with_the_certificate_the_sede_lets_us_in(self) -> None:
        identity = load_p12(self.sede.client_p12("NOMBRE DE PRUEBA - 00000000T"), "x")
        self.sidecar.configure([self.target], identity.spec("prueba"))
        response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.text, "Hola, NOMBRE DE PRUEBA - 00000000T")
        # Quitarlo vuelve a dejar la sede sin identificar.
        self.sidecar.configure([], None)
        self.assertEqual(self.get().status_code, 403)

    def test_a_host_outside_the_list_is_refused(self) -> None:
        # El proxy rechaza el CONNECT con 403, que httpx eleva como ProxyError.
        with httpx.Client(proxy=f"http://{self.host}", verify=False, timeout=15) as client:
            with self.assertRaises(httpx.ProxyError):
                client.get("https://example.com/")


if __name__ == "__main__":
    unittest.main()
