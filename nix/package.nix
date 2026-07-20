# Nix package for this pretix fork.
#
# Adapted from nixpkgs' pkgs/by-name/pr/pretix/package.nix. Keep the diff
# against nixpkgs small: when an upstream sync breaks this build, re-diff
# against nixpkgs' current recipe before fixing things by hand.
#
# Differences from nixpkgs:
#   - `src` is this repository itself (passed in by flake.nix); the version
#     is read from src/pretix/__init__.py.
#   - `npmDepsHash` must be bumped whenever package-lock.json changes; build
#     once and copy the correct hash from the mismatch error, or run
#     `nix run nixpkgs#prefetch-npm-deps -- package-lock.json`.
#   - The upstream test suite is not run at build time (doCheck = false);
#     tests run through `pretix-test` / CI instead.
#   - passthru.plugins is empty: our features are in-tree patches, not
#     plugins. Vendor nixpkgs' plugins/ directory here if that ever changes.
{
  lib,
  fetchFromGitHub,
  fetchPypi,
  fetchNpmDeps,
  nodejs,
  npmHooks,
  python3,
  gettext,
  pretix,
  src,
  plugins ? [ ],
}:

let
  versionLine = lib.lists.findFirst (line: lib.strings.hasPrefix "__version__" line) (throw
    "could not find __version__ in src/pretix/__init__.py"
  ) (lib.strings.splitString "\n" (builtins.readFile (src + "/src/pretix/__init__.py")));
  version = builtins.elemAt (builtins.match "__version__ = \"([^\"]+)\".*" versionLine) 0;

  python = python3.override {
    self = python;
    packageOverrides = self: super: {
      chardet = super.chardet_5;
      django = super.django_5;

      django-oauth-toolkit = super.django-oauth-toolkit.overridePythonAttrs (oldAttrs: rec {
        version = "2.3.0";
        src = fetchFromGitHub {
          inherit (oldAttrs.src) owner repo;
          tag = "v${version}";
          hash = "sha256-oGg5MD9p4PSUVkt5pGLwjAF4SHHf4Aqr+/3FsuFaybY=";
        };
        disabledTests = [
          # error message mismatch
          "test_validation_failed_message"
          # fails dns resolution
          "test_response_when_auth_server_response_return_404"
        ];
      });

      stripe = super.stripe.overridePythonAttrs rec {
        version = "7.9.0";

        src = fetchPypi {
          pname = "stripe";
          inherit version;
          hash = "sha256-hOXkMINaSwzU/SpXzjhTJp0ds0OREc2mtu11LjSc9KE=";
        };

        build-system = with self; [ setuptools ];
      };

      # our tree is ahead of nixpkgs' packaged pretix release
      django-querytagger = self.callPackage ./django-querytagger.nix { };
      django-redis = super.django-redis.overridePythonAttrs rec {
        version = "7.0.0";
        src = fetchPypi {
          pname = "django_redis";
          inherit version;
          hash = "sha256-5ISRyGL0NQsHR86xAWcAaG+5PE9OD7nEkP5sZlj/2TM=";
        };
        # the 6.x test setup doesn't carry over cleanly to 7.x
        doCheck = false;
      };

      pretix = self.toPythonModule pretix;
      pretix-plugin-build = self.callPackage ./plugin-build.nix { };
    };
  };
  pythonPackages = python.pkgs;
in
pythonPackages.buildPythonApplication (finalAttrs: {
  pname = "pretix";
  inherit src version;
  pyproject = true;

  patches = [
    # Discover pretix.plugin entrypoints during build and add them into
    # INSTALLED_APPS, so that their static files are collected.
    ./plugin-build.patch
  ];

  postPatch = ''
    # unused
    sed -i "/setuptools-rust/d" pyproject.toml

    # unbreak dependency relaxation
    substituteInPlace pyproject.toml \
      --replace-fail '"backend"' '"setuptools.build_meta"' \
      --replace-fail 'backend-path = ["_build"]' ""

    # we take care of the npm build
    substituteInPlace src/pretix/_build.py \
      --replace-fail "npm ci" "true" \
      --replace-fail "npm run build" "true"
  '';

  npmDeps = fetchNpmDeps {
    inherit (finalAttrs) src;
    hash = "sha256-DJCvNcgDIY71Q9qg4Ng7SAM9i9wHhHOdJonpt5t/Xx8=";
  };

  nativeBuildInputs = [
    nodejs
    npmHooks.npmConfigHook
  ];

  preBuild = ''
    npm run build
  '';

  build-system = with pythonPackages; [
    gettext
    nodejs
    setuptools
    tomli
  ];

  dependencies =
    with pythonPackages;
    [
      arabic-reshaper
      babel
      beautifulsoup4
      bleach
      celery
      chardet
      cryptography
      css-inline
      defusedcsv
      django
      django-bootstrap3
      django-compressor
      django-countries
      django-filter
      django-formset-js-improved
      django-formtools
      django-hierarkey
      django-hijack
      django-i18nfield
      django-libsass
      django-localflavor
      django-markup
      django-oauth-toolkit
      django-otp
      django-phonenumber-field
      django-querytagger
      django-redis
      django-scopes
      django-statici18n
      djangorestframework
      dnspython
      drf-ujson2
      geoip2
      importlib-metadata
      isoweek
      jsonschema
      kombu
      libsass
      lxml
      markdown
      mt-940
      oauthlib
      openpyxl
      packaging
      paypalrestsdk
      paypal-checkout-serversdk
      pyjwt
      phonenumberslite
      pillow
      pretix-plugin-build
      protobuf
      psycopg2-binary
      pycountry
      pycparser
      pycryptodome
      pypdf
      python-bidi
      python-dateutil
      pytz
      pytz-deprecation-shim
      pyuca
      qrcode
      redis
      reportlab
      requests
      sentry-sdk
      sepaxml
      stripe
      text-unidecode
      tlds
      tqdm
      ua-parser
      vat-moss
      vobject
      webauthn
      zeep
    ]
    ++ django.optional-dependencies.argon2
    ++ plugins;

  optional-dependencies = with pythonPackages; {
    memcached = [
      pylibmc
    ];
  };

  pythonRelaxDeps = [
    "beautifulsoup4"
    "bleach"
    "celery"
    "css-inline"
    "cryptography"
    "django-bootstrap3"
    "django-compressor"
    "django-filter"
    "django-formset-js-improved"
    "django-i18nfield"
    "django-localflavor"
    "django-phonenumber-field"
    "dnspython"
    "drf_ujson2"
    "importlib_metadata"
    "kombu"
    "markdown"
    "oauthlib"
    "phonenumberslite"
    "pillow"
    "protobuf"
    "pycparser"
    "pycryptodome"
    "pyjwt"
    "pypdf"
    "python-bidi"
    "qrcode"
    "redis"
    "reportlab"
    "requests"
    "sentry-sdk"
    "sepaxml"
    "ua-parser"
    "webauthn"
  ];

  pythonRemoveDeps = [
    "vat_moss_forked" # we provide a patched vat-moss package
  ];

  postInstall = ''
    mkdir -p $out/bin
    cp ./src/manage.py $out/${python.sitePackages}/pretix/manage.py
    makeWrapper $out/${python.sitePackages}/pretix/manage.py $out/bin/pretix-manage \
      --prefix PYTHONPATH : "$PYTHONPATH"

    # Trim packages size
    rm -rfv $out/${python.sitePackages}/pretix/static.dist/node_prefix
  '';

  dontStrip = true; # no binaries

  # The upstream test suite runs in this repo's own CI (pretix-test), not at
  # package build time.
  doCheck = false;

  passthru = {
    inherit
      python
      ;
    plugins = lib.recurseIntoAttrs { };
  };

  __structuredAttrs = true;

  meta = {
    description = "Ticketing software that cares about your event—all the way (the-efcc fork)";
    homepage = "https://github.com/the-efcc/pretix";
    license = with lib.licenses; [
      agpl3Only
      # 3rd party components below src/pretix/static
      bsd2
      isc
      mit
      ofl # fontawesome
      unlicense
      # all other files below src/pretix/static and src/pretix/locale and aux scripts
      asl20
    ];
    mainProgram = "pretix-manage";
    platforms = lib.platforms.linux;
  };
})
