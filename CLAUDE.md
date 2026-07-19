# CLAUDE.md

## This is a maintained fork

This repository is **the-efcc's fork of pretix**. We track upstream
(`pretix/pretix`) and continuously sync with it, while diverging to add our own
features on top.

Remotes:
- `origin` → `git@github.com:the-efcc/pretix.git` (our fork — the source of truth)
- `upstream` → `https://github.com/pretix/pretix.git` (read-only, for syncing)

**IMPORTANT: NEVER open a pull request against upstream (`pretix/pretix`).**
All PRs go to our fork: <https://github.com/the-efcc/pretix>.

## Branch model

| Branch | Role | Rules |
| --- | --- | --- |
| `master` | Pure mirror of `upstream/master` | Fast-forward only. Never commit to it, never force-push it. |
| `efcc` | The product branch: `master` + all our patches. This is what gets deployed. | Forward-only: never rebase it, never force-push it. Upstream changes land via merges of `master`. |
| feature branches | One per feature/fix, branched from `efcc` | Open PRs against `efcc`. Rebasing a feature branch is fine as long as nothing else is branched from it. |

**All feature work starts from `efcc`, and all PRs target `efcc`** — not
`master`. `master` exists only so we can fast-forward cleanly from upstream
and diff our divergence against pristine upstream at any time.

### Syncing with upstream

Run `scripts/sync-upstream.sh`. It fast-forwards `master` from
`upstream/master`, then merges `master` into `efcc`, and pushes both. Neither
branch is ever force-pushed, so open PRs are never disturbed by a sync.

If the merge into `efcc` conflicts, resolve the conflicts, commit, and push
`efcc`. Running `git config rerere.enabled true` once is recommended so
recurring conflict resolutions are remembered and replayed automatically.

## Development environment

Everything runs through [devenv](https://devenv.sh/). Enter the shell with
`devenv shell`, or run a single command via `devenv shell -- <command>`.

The following helper scripts are available inside the shell:

| Command | Purpose |
| --- | --- |
| `pretix-setup`  | Initialize the environment (build static files, run migrations) |
| `pretix-server` | Run the development server |
| `pretix-test`   | Run pytest (accepts args, e.g. `pretix-test -v path/to/test`) |
| `pretix-lint`   | Run code quality checks (flake8, isort, Django check) |
| `pretix-shell`  | Open the Django shell |

## Lint

```
devenv shell -- pretix-lint
```

Runs `flake8`, `isort -c`, and `python manage.py check` against `src/`.

## Test

```
devenv shell -- pretix-test
```

Runs `py.test` in `src/`. Pass extra args through, e.g.:

```
devenv shell -- pretix-test -v -k some_test
```

## Notes

- The Python code lives in `src/`; most manual commands run from there
  (`cd src && python manage.py <command>`).
- Frontend assets are built via `make -C src staticfiles`.

## Migrations

When a model changes, **generate migrations with Django** rather than writing
the migration files by hand:

```
devenv shell -- bash -c 'cd src && python manage.py makemigrations'
```

Then apply them with `python manage.py migrate`.
