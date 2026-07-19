#!/usr/bin/env bash
# Sync this fork with upstream pretix/pretix.
#
#   master  pure mirror of upstream/master (fast-forward only, never diverges)
#   efcc    product branch: master + all our patches (forward-only, deployed)
#
# Neither branch is ever rebased or force-pushed, so open PRs targeting
# `efcc` are never disturbed by a sync. Safe to re-run at any time.
#
# Requires the `upstream` remote:
#   git remote add upstream https://github.com/pretix/pretix.git
set -euo pipefail

git fetch upstream master
git fetch origin master efcc

# 1. Fast-forward the mirror. --ff-only aborts loudly if master has somehow
#    diverged from upstream (it never should — nobody commits to master).
git checkout -B master origin/master
git merge --ff-only upstream/master
git push origin master

# 2. Land the new upstream state on the product branch via a merge.
#    If this conflicts, resolve, `git commit`, then `git push origin efcc`.
git checkout -B efcc origin/efcc
git merge --no-edit master
git push origin efcc

echo "Sync complete: master mirrors upstream, efcc carries our patches on top."
