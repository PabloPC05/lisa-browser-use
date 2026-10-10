# arc-cua no está en nixpkgs: se instala la rueda publicada en PyPI, fijada por hash.
# Es Python puro; sus dependencias de macOS son opcionales y no se usan aquí.
{ python3Packages, fetchurl }:
python3Packages.buildPythonPackage {
  pname = "arc-cua";
  version = "0.1.1";
  format = "wheel";
  src = fetchurl {
    url = "https://files.pythonhosted.org/packages/bd/2a/cc1967991c47cb45f291d1d31521deed60fa07a16aa412849c5466743db1/arc_cua-0.1.1-py3-none-any.whl";
    sha256 = "5c74f4fc7a11ac9335f86d4be48d847230b5d8757d510dd9563caaf65723670b";
  };
  dependencies = with python3Packages; [ httpx h2 websockets ];
  doCheck = false;
  pythonImportsCheck = [ "arc_cua" ];
}
