#!/bin/sh
# Publish the allowlisted static export to the gh-pages branch (SRS §6/§7.2).
# Only web/ shell assets + dist/report.json are staged; the script refuses
# to proceed if any secret or local DB file appears in the staging dir.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

REPORT="dist/report.json"
[ -f "$REPORT" ] || {
    echo "error: $REPORT missing; run scripts/refresh.py first" >&2
    exit 1
}

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT

# Stage ONLY the public web shell + the allowlisted report.
cp web/index.html web/styles.css web/app.js web/manifest.webmanifest web/sw.js "$STAGE/"
cp "$REPORT" "$STAGE/report.json"

# Hard security boundary: no secrets or local databases leave the machine.
if find "$STAGE" \( -name '.env*' -o -name '*.sqlite3' -o -name '*.env' \) | grep -q .; then
    echo "refusing to publish: secret or database file in staging dir" >&2
    exit 1
fi

BRANCH="gh-pages"
WORKTREE="$STAGE/site"
if git show-ref --verify --quiet "refs/heads/$BRANCH"; then
    git worktree add --quiet "$WORKTREE" "$BRANCH"
else
    git worktree add --quiet --detach "$WORKTREE" HEAD
    (cd "$WORKTREE" && git checkout --quiet --orphan "$BRANCH" && git rm -rf . >/dev/null 2>&1 || true)
fi

find "$WORKTREE" -mindepth 1 -not -name '.git' -delete
cp "$STAGE"/index.html "$STAGE"/styles.css "$STAGE"/app.js \
   "$STAGE"/manifest.webmanifest "$STAGE"/sw.js "$STAGE"/report.json "$WORKTREE/"

cd "$WORKTREE"
git add -A
if git diff --cached --quiet; then
    echo "no changes to publish"
else
    git commit -q -m "PuckLuck daily report $(date -u +%Y-%m-%dT%H:%M:%SZ)"
    git push -u origin "$BRANCH"
    echo "published to $BRANCH"
fi
