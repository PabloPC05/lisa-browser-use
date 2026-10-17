"""Arranque del ejecutor: `python -m lisa_browser --config /etc/lisa-browser.toml`."""

from __future__ import annotations

import argparse
import asyncio
import logging
import tomllib
from pathlib import Path

from aiohttp import web
from arc_cua.policies import ChoicePolicy

from .certificate import Certificates, Sidecar
from .files import Files
from .jev import CommandCodeJevTransport
from .journal import Journal
from .library import Library
from .runner import Limits
from .server import Executor, Settings, make_app
from .session import Browser
from .vault import BitwardenCliVault


def _secret(path: str) -> str:
    return Path(path).read_text().strip()


def main() -> None:
    parser = argparse.ArgumentParser(prog="lisa_browser", description="Ejecutor de Browser Use de Lisa")
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)

    with open(args.config, "rb") as fh:
        cfg = tomllib.load(fh)
    jev, limits, paths = cfg["jev"], cfg.get("limits", {}), cfg["paths"]
    identity = cfg.get("identity", {})

    transport = CommandCodeJevTransport(
        api_key=_secret(jev["api_key_file"]), base_url=jev["base_url"], model=jev["model"],
        price_per_mtok=float(jev.get("price_per_mtok", 0.042)),
    )
    policy = ChoicePolicy(transport)

    vault = None
    if "vault" in cfg:
        v = cfg["vault"]
        vault = BitwardenCliVault(
            server=v["server"], client_id=_secret(v["client_id_file"]), client_secret=_secret(v["client_secret_file"]),
            master_password=_secret(v["master_password_file"]), data_dir=v["data_dir"], bw=v.get("bw", "bw"),
        )

    certificates = None
    if "certificate" in cfg:
        # Proxy de certificados (certificate.py). Chromium manda por él los dominios de `domains`
        # (lo decide su PAC, en la configuración del contenedor); con el certificado activo, los
        # intercepta para presentar el certificado de Pablo.
        c = cfg["certificate"]
        domains = [d.strip(".").lower() for d in c["domains"]]
        sidecar = Sidecar(
            c["sidecar"], proxy_key_pem=Path(c["proxy_key_file"]).read_text(), listen=c["listen"],
            allowed_peers=c.get("allowed_peers", ()), allowed_hosts=domains,
        )
        try:
            sidecar.start()
        except Exception:
            logging.getLogger(__name__).exception("el proxy de certificados no arrancó; se reintenta al usarlo")
        certificates = Certificates(sidecar, hosts=["." + d for d in domains])

    downloads, uploads = Path(paths["downloads"]), Path(paths["uploads"])
    executor = Executor(
        settings=Settings(
            token=_secret(cfg["token_file"]),
            money_domains=tuple(cfg.get("money_domains", ())),
            limits=Limits(int(limits.get("max_actions", 30)), float(limits.get("operation_seconds", 300))),
            approval_seconds=float(limits.get("approval_seconds", 600)),
            code_seconds=float(limits.get("code_seconds", 300)),
            price_per_mtok=transport.price_per_mtok,
        ),
        connect=lambda: Browser.connect(
            cfg["cdp_endpoint"], locale=identity.get("locale", ""), timezone=identity.get("timezone", ""),
        ),
        policy_for=lambda operation: policy,
        journal=Journal(paths["journal"]),
        files_for=lambda browser: Files(
            browser, downloads, uploads,
            downloads_dir_in_browser=paths.get("downloads_in_browser"), uploads_dir_in_browser=paths.get("uploads_in_browser"),
        ),
        uploads_dir=uploads,
        vault=vault,
        usage=transport.usage,
        library=Library(paths.get("library") or Path(paths["journal"]).with_name("biblioteca.sqlite")),
        certificates=certificates,
        # Fuera de la carpeta del contenedor del navegador: aquí no llega ninguna web.
        saved_downloads_dir=Path(paths.get("saved_downloads") or Path(paths["journal"]).with_name("descargas")),
    )
    host, _, port = cfg.get("listen", "127.0.0.1:8787").partition(":")
    if "mcp" not in cfg:
        web.run_app(make_app(executor), host=host, port=int(port), print=None)
        return
    # Con [mcp], el mismo proceso sirve también el MCP del navegador (mcp_server.py), que llama
    # directamente al ejecutor: misma cola, misma barrera y mismo registro.
    m = cfg["mcp"]
    asyncio.run(_serve_with_mcp(
        executor, host, int(port), m.get("listen", "127.0.0.1:8789"),
        token=_secret(m["token_file"]) if "token_file" in m else executor.settings.token,
        rules=Path(m.get("rules") or Path(paths["journal"]).with_name("reglas.json")),
    ))


async def _serve_with_mcp(executor: Executor, host: str, port: int, mcp_listen: str, *, token: str, rules: Path) -> None:
    import uvicorn

    from .mcp_server import BrowserMcp, Rules, make_asgi_app

    runner = web.AppRunner(make_app(executor))
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    mcp_host, _, mcp_port = mcp_listen.partition(":")
    browser_mcp = BrowserMcp(executor, Rules(rules), host=mcp_host, port=int(mcp_port))
    server = uvicorn.Server(uvicorn.Config(
        make_asgi_app(browser_mcp, token), host=mcp_host, port=int(mcp_port), log_level="warning", lifespan="on",
    ))
    try:
        # uvicorn atiende SIGTERM y SIGINT: al pararse, se cierra también la API.
        await server.serve()
    finally:
        await runner.cleanup()


if __name__ == "__main__":
    main()
