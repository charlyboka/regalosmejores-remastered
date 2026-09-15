#!/usr/bin/env bash
# Builds static/css/site.css with the Tailwind standalone CLI (Linux/macOS).
# See scripts/build_css.ps1 for the Windows equivalent.
set -euo pipefail

# Keep this in sync with TAILWIND_VERSION in .github/workflows/heroku-deploy.yml
VERSION="v4.3.3"
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TOOLS_DIR="$ROOT/.tools"
BINARY="$TOOLS_DIR/tailwindcss-$VERSION"

case "$(uname -s)-$(uname -m)" in
  Darwin-arm64)  ASSET="tailwindcss-macos-arm64" ;;
  Darwin-x86_64) ASSET="tailwindcss-macos-x64" ;;
  Linux-aarch64) ASSET="tailwindcss-linux-arm64" ;;
  *)             ASSET="tailwindcss-linux-x64" ;;
esac

if [ ! -x "$BINARY" ]; then
  mkdir -p "$TOOLS_DIR"
  echo "Downloading Tailwind CLI $VERSION ..."
  curl -sSL -o "$BINARY" \
    "https://github.com/tailwindlabs/tailwindcss/releases/download/$VERSION/$ASSET"
  chmod +x "$BINARY"
fi

mkdir -p "$ROOT/static/css"
OUTPUT="$ROOT/static/css/site.css"
"$BINARY" -i "$ROOT/static/src/input.css" -o "$OUTPUT" --minify "$@"

# A stylesheet with no utilities means the scan found no templates (or the build was cut short).
# Catch it here rather than in a browser looking at an unstyled page.
if ! grep -q "rounded-xl" "$OUTPUT"; then
  echo "ERROR: $OUTPUT contains no utility classes -- nothing was scanned." >&2
  exit 1
fi
echo "Built $OUTPUT ($(wc -c < "$OUTPUT") bytes)."
