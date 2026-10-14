# Proxy de certificados (sidecar/, Go solo con la biblioteca estándar): sin dependencias que bajar.
{ buildGoModule, lib }:
buildGoModule {
  pname = "lisa-sidecar";
  version = "0.1.0";
  src = lib.cleanSource ../sidecar;
  vendorHash = null;
  env.CGO_ENABLED = "0";
  # Las pruebas abren servidores TLS en 127.0.0.1: no caben en el aislamiento de compilación.
  doCheck = false;
  postInstall = "mv $out/bin/sidecar $out/bin/lisa-sidecar";
  meta.mainProgram = "lisa-sidecar";
}
