#!/usr/bin/env bash
set -euo pipefail

# Create a GitHub repo and push current repo to it.
# Usage:
#   GITHUB_USER=youruser REPO_NAME=gmail-crew-ai GITHUB_TOKEN=ghp_xxx ./scripts/create_github_repo.sh
# Or if gh CLI is installed and authenticated:
#   GITHUB_USER=youruser REPO_NAME=gmail-crew-ai ./scripts/create_github_repo.sh

GITHUB_USER=${GITHUB_USER:-}
REPO_NAME=${REPO_NAME:-crewai-gmail-automation}
PRIVATE=${PRIVATE:-true}

if [ -z "$GITHUB_USER" ]; then
  echo "GITHUB_USER is required" >&2
  exit 1
fi

if command -v gh >/dev/null 2>&1; then
  echo "Using gh CLI to create repo $GITHUB_USER/$REPO_NAME (private=$PRIVATE)"
  gh repo create "$GITHUB_USER/$REPO_NAME" --private --source=. --push
  exit 0
fi

if [ -z "${GITHUB_TOKEN:-}" ]; then
  echo "Neither gh CLI nor GITHUB_TOKEN found. Please set one of them." >&2
  exit 1
fi

echo "Creating GitHub repo via API..."
resp=$(curl -sS -H "Authorization: token $GITHUB_TOKEN" \
  -H "Accept: application/vnd.github+json" \
  https://api.github.com/user/repos \
  -d "{\"name\":\"$REPO_NAME\",\"private\":$PRIVATE}")

html_url=$(echo "$resp" | sed -n 's/.*"html_url" *: *"\(.*\)".*/\1/p' | head -n1)
clone_url=$(echo "$resp" | sed -n 's/.*"clone_url" *: *"\(.*\)".*/\1/p' | head -n1)

if [ -z "$clone_url" ]; then
  echo "Failed to create repo: $resp" >&2
  exit 1
fi

echo "Repo created: $html_url"

git remote remove origin >/dev/null 2>&1 || true
git remote add origin "$clone_url"
git push -u origin main
echo "Pushed to $html_url"

