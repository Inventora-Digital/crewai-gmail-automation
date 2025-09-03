#!/usr/bin/env bash
set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: scripts/update-openapi-url.sh <BASE_URL> <OPENAPI_FILE>" >&2
  exit 1
fi

BASE_URL="$1"
FILE="$2"

if [[ ! -f "$FILE" ]]; then
  echo "OpenAPI file not found: $FILE" >&2
  exit 1
fi

# Replace the first servers url in the OpenAPI file
# Cross-platform sed handling (GNU vs BSD)
if sed --version >/dev/null 2>&1; then
  # GNU sed
  sed -i -E "s|(^\s*- url:)\s*.*|\1 ${BASE_URL}|" "$FILE"
else
  # BSD/macOS sed
  sed -i '' -E "s|(^\s*- url:)\s*.*|\1 ${BASE_URL}|" "$FILE"
fi

echo "Patched servers.url -> ${BASE_URL} in ${FILE}"

