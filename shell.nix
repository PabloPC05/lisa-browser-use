# Entorno de desarrollo y pruebas: `nix-shell --run "python -m unittest discover -s tests -t ."`
# En NixOS, <nixpkgs> es el nixpkgs fijado por la configuración del sistema.
{ pkgs ? import <nixpkgs> { } }:
let
  arc-cua = pkgs.callPackage ./nix/arc-cua.nix { };
  sidecar = pkgs.callPackage ./nix/sidecar.nix { };
  python = pkgs.python3.withPackages (ps: [ arc-cua ps.aiohttp ps.httpx ps.h2 ps.websockets ps.tldextract ps.cryptography ]);
in
pkgs.mkShell {
  packages = [ python pkgs.chromium pkgs.go ];
  shellHook = ''
    export PYTHONPATH=$PWD/src
    export ARC_CHROME=${pkgs.chromium}/bin/chromium
    export LISA_SIDECAR=${sidecar}/bin/lisa-sidecar
  '';
}
