#!/usr/bin/env bash
# Turn the current tip of the private main branch into a pull request on the public repository.
#
# Usage: scripts/export_public.sh [--stage | --publish] <path of a clone of the public repository>
#
# Without a mode both steps run. `--stage` stops after the commit on the new branch (nothing is
# pushed) and prints the branch name as its last line; `--publish` pushes the branch the clone
# currently has checked out and opens the pull request. scripts/public_transfer.py runs the audit
# between the two.
#
# Run it from a checkout of the private main branch with a clean tree. It never pushes the private
# history: the public clone gets one commit holding this tree, on a new branch, and the pull
# request is the only way the change reaches the public main branch. The maintainer's pre-push
# hook (gitleaks plus the deny list over the pushed commits) runs on the push; the pull request
# then has to pass the required checks (see docs/public-release.md).
set -euo pipefail

public_repo="Abrak98/proc-jobs"
author_name="Abrak98"
author_email="144574389+Abrak98@users.noreply.github.com"

mode=all
case "${1:-}" in
  --stage) mode=stage; shift ;;
  --publish) mode=publish; shift ;;
esac
if [ "$#" -ne 1 ]; then
  echo "usage: $0 [--stage | --publish] <path of a clone of ${public_repo}>" >&2
  exit 2
fi
clone=$(realpath "$1")
source_repo=$(git rev-parse --show-toplevel)

if [ "$mode" != publish ] && [ -n "$(git -C "$source_repo" status --porcelain --untracked-files=no)" ]; then
  echo "export_public: the private checkout has uncommitted changes" >&2
  exit 1
fi
origin_url=$(git -C "$clone" remote get-url origin)
case "$origin_url" in
  "git@github.com:${public_repo}.git" | "https://github.com/${public_repo}" | "https://github.com/${public_repo}.git") ;;
  *)
    echo "export_public: ${clone} is not a clone of ${public_repo} (origin is ${origin_url})" >&2
    exit 1
    ;;
esac
if [ -n "$(git -C "$clone" status --porcelain)" ]; then
  echo "export_public: the public clone has uncommitted changes" >&2
  exit 1
fi

if [ "$mode" != publish ]; then
  short=$(git -C "$source_repo" rev-parse --short=10 HEAD)
  branch="sync-${short}"
  git -C "$clone" fetch --quiet origin main
  git -C "$clone" switch --quiet -c "$branch" origin/main

  # Replace the whole tree: files deleted in the private repository are deleted in the public one.
  git -C "$clone" rm -r --quiet --ignore-unmatch -- .
  git -C "$source_repo" archive HEAD | tar -x -C "$clone"
  git -C "$clone" add -A -- .

  if git -C "$clone" diff --cached --quiet; then
    echo "export_public: the public main branch already has this tree; nothing to send"
    git -C "$clone" switch --quiet main
    git -C "$clone" branch --quiet -D "$branch"
    exit 0
  fi

  subject="Sync the tree to ${short}: $(git -C "$source_repo" log -1 --format=%s)"
  GIT_AUTHOR_NAME="$author_name" GIT_AUTHOR_EMAIL="$author_email" \
    GIT_COMMITTER_NAME="$author_name" GIT_COMMITTER_EMAIL="$author_email" \
    git -C "$clone" commit --quiet -m "$subject"
  if [ "$mode" = stage ]; then
    echo "$branch"
    exit 0
  fi
fi

if [ "$mode" = publish ]; then
  branch=$(git -C "$clone" rev-parse --abbrev-ref HEAD)
  short=${branch#sync-}
  subject=$(git -C "$clone" log -1 --format=%s)
fi

# The pre-push hook of the maintainer's machine scans the commit again before it leaves.
git -C "$clone" push --quiet -u origin "$branch"
gh pr create --repo "$public_repo" --head "$branch" --base main \
  --title "$subject" --body "Sync of the maintained tree at ${short}."
