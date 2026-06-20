{
  pkgs,
  config,
  ...
}:

let
  # The Python virtualenv lives here. Prepending it to PATH makes the scripts
  # self-contained: `python`/`./manage.py` resolve to the venv interpreter even
  # when a script is invoked outside an activated devenv shell (otherwise the
  # bare Nix python3 — which has no Django — gets picked up via the shebang).
  venvBin = "${config.env.DEVENV_STATE}/venv/bin";
in
{
  # Base packages and environment
  packages = with pkgs; [
    # Build dependencies
    libffi
    openssl
    libxml2
    libxslt
    enchant_2
    gettext
    postgresql

    # Development tools
    git
    gnumake
  ];

  # Python environment with dev dependencies
  languages.python = {
    enable = true;
    version = "3.11";
    venv.enable = true;
    venv.requirements = ''
      # Install pretix in editable mode with dev dependencies
      -e .[dev]
    '';
  };

  # Node.js for frontend assets
  languages.javascript = {
    enable = true;
    package = pkgs.nodejs_20;
    npm.enable = true;
  };

  # PostgreSQL database for development
  services.postgres = {
    enable = true;
    initialDatabases = [ { name = "pretix"; } ];
    # Create a dedicated role so the connection config doesn't depend on the
    # OS username. Runs only when the cluster is first initialized.
    initialScript = "CREATE USER pretix SUPERUSER;";
    listen_addresses = "127.0.0.1";
  };

  # Environment variables
  env = {
    PRETIX_DATA_DIR = "${config.env.DEVENV_STATE}/pretix-data";
    DATA_DIR = "${config.env.DEVENV_STATE}/pretix-data";
    PGDATA = "${config.env.DEVENV_STATE}/postgres";

    # pretix reads DB config from PRETIX_<SECTION>_<OPTION> env vars
    # (it does NOT understand DATABASE_URL). Point it at the local postgres
    # service; trust auth on 127.0.0.1 means no password is needed.
    PRETIX_DATABASE_BACKEND = "postgresql";
    PRETIX_DATABASE_NAME = "pretix";
    PRETIX_DATABASE_USER = "pretix";
    PRETIX_DATABASE_HOST = "127.0.0.1";
    PRETIX_DATABASE_PORT = "5432";
  };

  # Scripts for common development tasks
  scripts = {
    pretix-setup.exec = ''
      set -e
      export PATH="${venvBin}:$PATH"
      echo "Setting up pretix development environment..."

      # Create data directory
      mkdir -p $PRETIX_DATA_DIR

      # Build frontend assets and collect static files.
      # The Makefile lives in src/ and its `staticfiles` target runs
      # npm ci, npm run build, compilejsi18n (which compiles locales) and collectstatic.
      echo "Building static files (npm install, npm build, locales, collectstatic)..."
      make -C src staticfiles

      # Run migrations
      echo "Running database migrations..."
      (cd src && python manage.py migrate)

      echo "Setup complete! Default admin user: admin@localhost / admin"
    '';

    pretix-server.exec = ''
      export PATH="${venvBin}:$PATH"
      echo "Starting pretix development server..."
      cd src && python manage.py runserver
    '';

    pretix-test.exec = ''
      export PATH="${venvBin}:$PATH"
      echo "Running pytest..."
      cd src && py.test "$@"
    '';

    pretix-lint.exec = ''
      export PATH="${venvBin}:$PATH"
      echo "Running code quality checks..."
      cd src
      echo "Running flake8..."
      flake8 .
      echo "Running isort..."
      isort -c .
      echo "Running Django checks..."
      python manage.py check
      echo "All checks passed!"
    '';

    pretix-shell.exec = ''
      export PATH="${venvBin}:$PATH"
      cd src && python manage.py shell
    '';
  };

  # Tasks that run automatically
  tasks = {
    "pretix:venv-setup" = {
      exec = ''
        echo "Python virtual environment ready"
      '';
      after = [ "devenv:python:virtualenv" ];
    };
  };

  # Enter shell hook
  enterShell = ''
    echo "🎫 pretix development environment"
    echo ""
    echo "Available commands:"
    echo "  pretix-setup      - Initialize the development environment"
    echo "  pretix-server     - Run the development server"
    echo "  pretix-test       - Run pytest (pass args: pretix-test -v)"
    echo "  pretix-lint       - Run code quality checks (flake8, isort, Django check)"
    echo "  pretix-shell      - Open Django shell"
    echo ""
    echo "Manual commands:"
    echo "  cd src && python manage.py <command>"
    echo "  make npminstall                        - Install npm dependencies"
    echo "  make localecompile                     - Compile translations"
    echo ""

    # Check if setup has been run
    if [ ! -d "src/static.dist" ]; then
      echo "⚠️  First time setup required. Run: pretix-setup"
      echo ""
    fi
  '';

  # Pre-commit hooks for code quality
  # git-hooks.hooks = {
  #   flake8.enable = true;
  #   isort.enable = true;
  # };
}
