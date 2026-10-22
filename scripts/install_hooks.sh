#!/bin/sh
# Point git at the versioned hooks in scripts/git-hooks.
set -e
cd "$(git rev-parse --show-toplevel)"
git config core.hooksPath scripts/git-hooks
chmod +x scripts/git-hooks/* 2>/dev/null || true
echo "hooks path: $(git config core.hooksPath)"
