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
| `efcc` | The product branch: `master` + all our patches. This is what gets deployed. | Forward-only: never rebase it, never force-push it. Upstream changes land via merges of upstream *release tags*. |
| feature branches | One per feature/fix, branched from `efcc` | Open PRs against `efcc`. Rebasing a feature branch is fine as long as nothing else is branched from it. |

**All feature work starts from `efcc`, and all PRs target `efcc`** — not
`master`. `master` exists only so we can fast-forward cleanly from upstream
and diff our divergence against pristine upstream at any time.

### Syncing with upstream

`efcc` follows upstream **release tags**, not upstream's master tip — and only
once that release is the version packaged in nixpkgs, because the fork is
deployed with nixpkgs' pretix package (with `src` overridden to `efcc`).
Syncing past the packaged version would make `efcc` undeployable until nixpkgs
catches up.

Before syncing, confirm the target release is in nixpkgs:

```
nix eval nixpkgs#pretix.version
```

Then run:

```
scripts/sync-upstream.sh v2026.7.0
```

It fast-forwards the `master` mirror from `upstream/master`, then merges the
given release tag into `efcc`, repairs the Django migration graph, and pushes
both. Neither branch is ever force-pushed, so open PRs are never disturbed by
a sync. It needs `devenv` on PATH for the migration step, and refuses to push
`efcc` without it.

> **One-time note:** `efcc` carries a downgrade commit (`a0504e1c5`) that
> pinned its tree back to v2026.6.0 while its history already contained newer
> upstream commits. Before merging the next release tag, revert it
> (`git revert a0504e1c5`) — `sync-upstream.sh` refuses to run until then.
> Remove this note and the script guard once that's done.

If the merge into `efcc` conflicts, resolve the conflicts, commit, and re-run
the script — the merge becomes a no-op and the migration step still runs.
Don't push `efcc` by hand, or you may publish a tree that cannot migrate.
Running `git config rerere.enabled true` once is recommended so recurring
conflict resolutions are remembered and replayed automatically.

### Merge migrations

Whenever we carry a migration in an app upstream also migrates (`pretixbase`),
theirs and ours hang off the same parent when a release adds one, and the app
is left with two leaf nodes — at which point Django refuses to run *any*
migration. The sync script resolves this by running `makemigrations --merge`
and committing the resulting merge migration, which carries no operations and
only re-joins the branches. Upstream's own history has several (`0022_merge`,
`0035_merge`, `0174_merge_20201222_1031`).

The merge migration is a workaround, not the plan — see below.

### Our schema lives in the `pretix.efcc` app

`src/pretix/efcc/` is a Django app whose only job is to own our schema. It has
its own app label (`efcc`) and therefore its own migration graph, which
upstream never writes to. **New models we add go there, not in `pretixbase`.**

That is what keeps syncs boring: a `pretixbase` migration of ours is a leaf
that collides with upstream's next one on *every* release, forever. A migration
in `efcc` never collides with anything.

Two rules follow from this:

* **Never run `makemigrations pretixbase`.** Add the model to
  `pretix/efcc/models.py` and run
  `devenv shell -- bash -c 'cd src && python manage.py makemigrations efcc'`.
* **Never add a field to an upstream model.** A field on `Order`, `Voucher`,
  `OrderPayment`, … is a `pretixbase` migration no matter which app you put the
  model code in — Django binds an operation to the app label of the migration
  that contains it. Model the extension as a table in `efcc` with a
  `ForeignKey`/`OneToOneField` back to the upstream model instead, and reach it
  through the reverse accessor (`order.installment_plan`).

`efcc.0001_initial` depends on a `pretixbase` node. Depending *into*
`pretixbase` is fine — it only orders the two graphs, it does not create a leaf
there. Point new dependencies at a pristine-upstream migration rather than at
one of ours.

Editing upstream *code* (a hook in `OrderPayment.confirm()`, an extra import)
is unavoidable and cheap: git merges it. Only schema is structural, and only
schema needs this discipline.

One exception is still outstanding: `Voucher.valid_if_pending`
(`pretixbase/0307`) predates this rule and is still a `pretixbase` leaf, so
expect one merge migration per sync until it is moved.

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

Our models live in the `pretix.efcc` app, and so do their migrations — see
"Our schema lives in the `pretix.efcc` app" above for why, and for the two
rules that keep it that way.

When a model changes, **generate migrations with Django** rather than writing
the migration files by hand:

```
devenv shell -- bash -c 'cd src && python manage.py makemigrations efcc'
```

Then apply them with `python manage.py migrate`. Naming the app is deliberate:
a bare `makemigrations` will happily write into `pretixbase` too.
