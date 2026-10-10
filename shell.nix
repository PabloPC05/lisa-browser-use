# Entorno de desarrollo y pruebas: `nix-shell --run "python -m unittest discover -s tests -t ."`
# En NixOS, <nixpkgs> es el nixpkgs fijado por la configuración del sistema.
{ pkgs ? import <nixpkgs> { } }:
let
  arc-cua = pkgs.callPackage ./nix/arc-cua.nix { };
  python = pkgs.python3.withPackages (ps: [ arc-cua ps.aiohttp ps.httpx ps.h2 ps.websockets ps.tldextract ]);
in
pkgs.mkShell {
  packages = [ python pkgs.chromium ];
  shellHook = ''
    export PYTHONPATH=$PWD/src
    export ARC_CHROME=${pkgs.chromium}/bin/chromium
  '';
}
