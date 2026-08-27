#!/usr/bin/env bash
# Sync this fork with upstream pretix/pretix.
#
#   master  pure mirror of upstream/master (fast-forward only, never diverges)
#   efcc    product branch: master + all our patches (forward-only, deployed)
#
# Policy: efcc follows upstream *release tags*, and only once that release is
# the version packaged in nixpkgs (check: nix eval nixpkgs#pretix.version) —
# the fork is deployed with nixpkgs' pretix package, so syncing past the
# packaged version would make efcc undeployable until nixpkgs catches up.
#
#   scripts/sync-upstream.sh v2026.7.0
#
# Neither long-lived branch is ever rebased or force-pushed, so open PRs
# targeting efcc are never disturbed by a sync. Safe to re-run at any time.
#
# Requires the `upstream` remote:
#   git remote add upstream https://github.com/pretix/pretix.git
set -euo pipefail

tag="${1:?usage: $0 <release-tag>   e.g.: $0 v2026.7.0}"

# The script moves between branches with `checkout -B`, which happily drags
# uncommitted work along with it. Refuse to start rather than scatter it.
if ! git diff --quiet || ! git diff --cached --quiet; then
  echo "ERROR: you have uncommitted changes. Commit or stash them first." >&2
  exit 1
fi
if [ -e "$(git rev-parse --git-dir)/MERGE_HEAD" ]; then
  echo "ERROR: a merge is still in progress. Resolve the conflicts and commit," >&2
  echo "then re-run this script." >&2
  exit 1
fi

git fetch upstream master "refs/tags/$tag:refs/tags/$tag"
git fetch origin master efcc

# 1. Fast-forward the mirror; fails loudly if master ever diverges (it never
#    should — nobody commits to master). The mirror tracks upstream's
#    development tip; only efcc is pinned to release tags.
git checkout -B master origin/master
git merge --ff-only upstream/master
git push origin master

# 2. Land the release on the product branch via a merge.
#    If this conflicts, resolve, `git commit`, then re-run this script — the
#    merge turns into a no-op and step 3 still gets to run. Don't push
#    by hand here, or you may push an efcc that cannot migrate.
#
#    That recovery only works if re-running keeps the resolved merge commit,
#    which lives nowhere but this checkout until step 4 pushes it. So local
#    efcc is only reset to the remote when it does not already contain it;
#    if it is ahead, it is a resolution from a previous run and is kept.
if git rev-parse --verify --quiet efcc >/dev/null &&
   git merge-base --is-ancestor origin/efcc efcc; then
  git checkout efcc
else
  git checkout -B efcc origin/efcc
fi

if git merge-base --is-ancestor "$tag" HEAD; then
  echo "efcc already contains $tag — skipping the merge."
else
  git merge --no-edit "$tag"
fi

# 3. Repair the Django migration graph.
#    We carry our own migrations in apps upstream also migrates (pretixbase),
#    so whenever a release adds one, both theirs and ours hang off the same
#    parent and the app ends up with two leaf nodes. Django then refuses to
#    run *any* migration:
#
#      Conflicting migrations detected; multiple leaf nodes in the migration
#      graph: (0307_upstream_next, 0307_voucher_valid_if_pending in pretixbase)
#
#    `--merge` writes a merge migration with no operations that re-joins the
#    branches. It only ever merges; it never picks up unrelated model drift,
#    so it is safe to run unconditionally. Expect this on most syncs.
if [ "${SKIP_MIGRATION_MERGE:-}" = "1" ]; then
  echo "WARNING: skipping the migration merge (SKIP_MIGRATION_MERGE=1)." >&2
  echo "WARNING: efcc may have a forked migration graph." >&2
elif ! command -v devenv >/dev/null 2>&1; then
  echo "ERROR: devenv not found, cannot check the migration graph." >&2
  echo "efcc has been merged locally but NOT pushed. Either install devenv and" >&2
  echo "re-run this script, or run the check by hand:" >&2
  echo "  cd src && python manage.py makemigrations --merge" >&2
  echo "then commit anything it generates and push efcc yourself." >&2
  exit 1
else
  devenv shell -- bash -c 'cd src && python manage.py makemigrations --merge --noinput'
  merge_migrations="$(git ls-files --others --exclude-standard -- '*/migrations/*.py')"
  if [ -n "$merge_migrations" ]; then
    printf '%s\n' "$merge_migrations" | xargs git add --
    git commit -q -m "Merge migration graph after syncing $tag" \
      -m "Upstream and this fork both added migrations on top of the same parent, leaving the app with two leaf nodes. This merge migration has no operations; it only re-joins the branches so migrate can run again."
    echo "Added a merge migration:"
    printf '  %s\n' $merge_migrations
  fi
fi

# 4. Publish the result.
git push origin efcc

echo "Synced: master mirrors upstream, efcc now includes $tag."
