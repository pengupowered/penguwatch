#!/usr/bin/env bash
# Tests the PenguWatch image the way it is run: read-only root, unprivileged user, a data volume, a config mounted in,
# and no network at all. Podman by default (CONTAINER=docker works too); the same script runs locally and in CI.
#
#   scripts/test-container.sh                   build the image from source (what scripts/deploy.sh does), test it
#   scripts/test-container.sh --prebuilt        build it from the wheel in dist/ (WHEEL_STAGE=wheel-prebuilt, as CI does)
#   scripts/test-container.sh --image NAME      test an image that already exists
#
# Checks: uid 10001; root filesystem read-only (/app is not writable, /app/data and /tmp are); the installed package is
# the wheel's (no source tree in the image); `penguwatch --version` matches src/penguwatch/__init__.py; the mounted
# config.yaml is the one in use; /api/health answers 200 and the app reports that version, asked from inside the container (the
# container has --network none); the database is created in the volume; a SIGTERM stops it cleanly.
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ENGINE="${CONTAINER:-podman}"
IMAGE="localhost/penguwatch-ci:$(basename "$ROOT" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9_.\n-' '-')"
BUILD_ARGS=()
BUILD=1
while [ $# -gt 0 ]; do
    case "$1" in
        --prebuilt) BUILD_ARGS=(--build-arg WHEEL_STAGE=wheel-prebuilt) ;;
        --image) IMAGE="${2:?--image needs a name}"; BUILD=0; shift ;;
        -h|--help) sed -n '2,14p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $1" >&2; exit 2 ;;
    esac
    shift
done

fail() { echo "FAIL: $*" >&2; exit 1; }
ok() { echo "ok: $*"; }

version="$(sed -n 's/^__version__ = "\(.*\)"$/\1/p' "$ROOT/src/penguwatch/__init__.py" | head -n1)"
[ -n "$version" ] || fail "cannot read __version__"

if [ "$BUILD" = 1 ]; then
    "$ENGINE" build "${BUILD_ARGS[@]}" -t "$IMAGE" "$ROOT"
fi

tmp="$(mktemp -d)"
name="penguwatch-test-$$"
cleanup() { "$ENGINE" rm -f "$name" >/dev/null 2>&1 || true; "$ENGINE" volume rm -f "$name-data" >/dev/null 2>&1 || true; rm -rf "$tmp"; }
trap cleanup EXIT
cat > "$tmp/config.yaml" <<'YAML'
household:
  members: ["Test"]
services:
  - {id: "svc_test", name: "Container Test Service", monthly_cost: 1.0, is_active: false}
YAML
chmod 644 "$tmp/config.yaml"

"$ENGINE" volume create "$name-data" >/dev/null
# The same restrictions as compose: read-only root, a tmpfs, no capabilities, no new privileges; plus no network.
"$ENGINE" run -d --name "$name" --network none --read-only --tmpfs /tmp --cap-drop ALL \
    --security-opt no-new-privileges:true -v "$name-data:/app/data" -v "$tmp/config.yaml:/app/config.yaml:ro,Z" \
    "$IMAGE" >/dev/null

in_container() { "$ENGINE" exec "$name" "$@"; }

[ "$(in_container id -u)" = 10001 ] || fail "not running as uid 10001"
ok "runs as uid 10001"
in_container sh -c 'touch /app/probe 2>/dev/null' && fail "/app is writable" || ok "/app is read-only"
in_container sh -c 'touch /app/data/probe /tmp/probe && rm /app/data/probe /tmp/probe' || fail "/app/data or /tmp not writable"
ok "/app/data and /tmp are writable"
in_container sh -c 'test ! -e /app/src && test ! -e /app/tests' || fail "a source tree is in the image"
ok "no source tree in the image"
[ "$(in_container penguwatch --version)" = "penguwatch $version" ] || fail "penguwatch --version is not $version"
ok "penguwatch --version is $version"

health=""
for _ in $(seq 1 60); do
    health="$(in_container python -c "import urllib.request as u; r = u.urlopen('http://127.0.0.1:8080/api/health', timeout=2); print(r.status, r.read().decode())" 2>/dev/null)" && break
    health=""
    [ "$("$ENGINE" inspect -f '{{.State.Running}}' "$name")" = true ] || { "$ENGINE" logs "$name" >&2; fail "the container stopped"; }
    sleep 1
done
[ -n "$health" ] || { "$ENGINE" logs "$name" >&2; fail "/api/health did not answer within 60 s"; }
case "$health" in '200 {"status":"ok"}') ok "/api/health answers 200" ;; *) fail "unexpected /api/health: $health" ;; esac
served="$(in_container python -c "import json, urllib.request as u; print(json.load(u.urlopen('http://127.0.0.1:8080/openapi.json', timeout=5))['info']['version'])")"
[ "$served" = "$version" ] || fail "the app reports version $served, not $version"
ok "the app reports version $version"

in_container python -c "
import json, urllib.request as u
names = [s['name'] for s in json.load(u.urlopen('http://127.0.0.1:8080/api/services', timeout=5))]
raise SystemExit(0 if 'Container Test Service' in names else 1)" || fail "the mounted config.yaml is not in use"
ok "the mounted config.yaml is in use"
in_container test -f /app/data/penguwatch.db || fail "no database in /app/data"
ok "the database is in the data volume"

"$ENGINE" stop -t 20 "$name" >/dev/null
[ "$("$ENGINE" inspect -f '{{.State.ExitCode}}' "$name")" = 0 ] || fail "did not exit 0 on SIGTERM"
ok "stops cleanly on SIGTERM"
echo "container test passed ($IMAGE)"
