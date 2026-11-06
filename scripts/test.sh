#!/usr/bin/env bash
# Builds the test image (runtime + dev deps + tests) and runs the suite in a throwaway container. Extra args go to
# pytest.
#   scripts/test.sh                      whole suite
#   scripts/test.sh --seed               also mounts the sanitized library (.seed) at /seed and sets
#                                        PENGUWATCH_SEED_DIR, which enables the characterization tests
#   scripts/test.sh --command <cmd...>   run this command in the container instead of pytest
#                                        (e.g. --seed --command python -m tests.characterization record ...)
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
# One image per checkout, so test runs in parallel work trees never use each other's build
IMAGE="localhost/penguwatch-test:$(basename "$ROOT" | tr 'A-Z' 'a-z' | tr -c 'a-z0-9_.\n-' '-')"

run_args=(--rm --network none)
cmd=(python -m pytest -q -p no:cacheprovider)
if [ "${1:-}" = "--seed" ]; then
    shift
    [ -d "$ROOT/.seed" ] || { echo "no .seed/ library in $ROOT" >&2; exit 2; }
    run_args+=(-v "$ROOT/.seed:/seed:Z" -e PENGUWATCH_SEED_DIR=/seed)
fi
if [ "${1:-}" = "--command" ]; then
    shift
    cmd=("$@")
    set --
fi

podman build -q -f "$ROOT/Dockerfile.test" -t "$IMAGE" "$ROOT" >/dev/null
exec podman run "${run_args[@]}" "$IMAGE" "${cmd[@]}" "$@"
