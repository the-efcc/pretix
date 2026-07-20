# Not (yet) packaged in nixpkgs; needed by pretix since upstream's
# "Add query tagging for periodic tasks".
{
  lib,
  buildPythonPackage,
  fetchPypi,
  setuptools,
}:

buildPythonPackage rec {
  pname = "django-querytagger";
  version = "0.0.3";
  pyproject = true;

  src = fetchPypi {
    pname = "django_querytagger";
    inherit version;
    hash = "sha256-Zjm4ZKb6KtK7Mnn4g8hY26ogKLnweVer8BHqa6gUmSI=";
  };

  build-system = [
    setuptools
  ];

  doCheck = false; # no tests

  meta = {
    description = "Tag Django SQL queries with comments identifying their origin";
    homepage = "https://pypi.org/project/django-querytagger/";
    license = lib.licenses.asl20;
  };
}
