"""Arranque del ejecutor: `python -m lisa_browser --config /etc/lisa-browser.toml`."""

from __future__ import annotations

import argparse
import logging
import tomllib
from pathlib import Path

from aiohttp import web
from arc_cua.policies import ChoicePolicy

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
        connect=lambda: Browser.connect(cfg["cdp_endpoint"]),
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
    )
    host, _, port = cfg.get("listen", "127.0.0.1:8787").partition(":")
    web.run_app(make_app(executor), host=host, port=int(port), print=None)


if __name__ == "__main__":
    main()
