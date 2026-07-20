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

git fetch upstream master "refs/tags/$tag:refs/tags/$tag"
git fetch origin master efcc

# One-time guard: efcc carries a downgrade commit that pinned its tree back
# to v2026.6.0 while its history already contained newer upstream commits.
# It MUST be reverted before the next release merge, or the merge will
# silently strip the v2026.6.0..master upstream changes from the result.
# Remove this guard once the revert has landed.
DOWNGRADE_COMMIT=a0504e1c5928c9e681414234ddebcfcfab845c00
if git merge-base --is-ancestor "$DOWNGRADE_COMMIT" origin/efcc &&
   ! git log origin/efcc --grep="reverts commit $DOWNGRADE_COMMIT" --oneline | grep -q .; then
  echo "ERROR: revert the v2026.6.0 downgrade before syncing:" >&2
  echo "  git checkout efcc && git revert $DOWNGRADE_COMMIT && git push origin efcc" >&2
  echo "then re-run this script (and remove this guard)." >&2
  exit 1
fi

# 1. Fast-forward the mirror; fails loudly if master ever diverges (it never
#    should — nobody commits to master). The mirror tracks upstream's
#    development tip; only efcc is pinned to release tags.
git checkout -B master origin/master
git merge --ff-only upstream/master
git push origin master

# 2. Land the release on the product branch via a merge.
#    If this conflicts, resolve, `git commit`, then `git push origin efcc`.
git checkout -B efcc origin/efcc
git merge --no-edit "$tag"
git push origin efcc

echo "Synced: master mirrors upstream, efcc now includes $tag."
