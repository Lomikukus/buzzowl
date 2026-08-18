#!/usr/bin/env bash
# Build the hardened browser image the research agents fetch pages with.
#
# Camofox cannot be built straight from its Git URL: the upstream Dockerfile
# bind-mounts browser binaries that `make fetch` downloads first, and those are
# not in the Git context. This script does the clone + build once.
#
#   ./scripts/build-browser.sh            # auto-detects the host architecture
#   CAMOFOX_ARCH=x86_64 ./scripts/...     # or force one
#
# Result: image camofox-browser:135.0.1-<arch> (~2.5 GB), which docker-compose.yml
# references. Skip this and start without the browser containers instead:
#   docker compose up -d db searxng server agent-pi
set -euo pipefail

TAG_VERSION="135.0.1"
SRC_DIR="${CAMOFOX_SRC:-./camofox-browser}"
UPSTREAM="https://github.com/jo-inc/camofox-browser"

case "${CAMOFOX_ARCH:-$(uname -m)}" in
  arm64|aarch64) ARCH=aarch64 ;;
  x86_64|amd64)  ARCH=x86_64 ;;
  *) echo "Unsupported architecture: $(uname -m)" >&2; exit 1 ;;
esac

IMAGE="camofox-browser:${TAG_VERSION}-${ARCH}"
if docker image inspect "$IMAGE" >/dev/null 2>&1; then
  echo "$IMAGE already exists — nothing to do."
  exit 0
fi

command -v docker >/dev/null || { echo "docker is required" >&2; exit 1; }
command -v git    >/dev/null || { echo "git is required" >&2; exit 1; }
command -v make   >/dev/null || { echo "make is required (Xcode CLT / build-essential)" >&2; exit 1; }

if [ ! -d "$SRC_DIR/.git" ]; then
  echo "→ cloning $UPSTREAM into $SRC_DIR"
  git clone --depth 1 "$UPSTREAM" "$SRC_DIR"
fi

echo "→ building $IMAGE (a few minutes, downloads ~500 MB of browser binaries)"
if [ "$ARCH" = aarch64 ]; then
  make -C "$SRC_DIR" build-arm64
else
  make -C "$SRC_DIR" build-x86
  echo "→ remember to set CAMOFOX_ARCH=x86_64 in .env"
fi

docker image inspect "$IMAGE" >/dev/null 2>&1 \
  && echo "✓ $IMAGE ready — now run: docker compose up -d" \
  || { echo "Build finished but $IMAGE is missing — check the output above." >&2; exit 1; }
