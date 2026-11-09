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

# The container is locked down (the tests run dependency code, and a test-only package must not reach the host):
#   --timeout          podman itself kills the container after this long, also when this script was killed
#   --network none     the tests only use loopback; no route out
#   --cap-drop=ALL, no-new-privileges   no capabilities, no privilege gain (the image's user, 10001, is not root either)
#   --read-only        immutable root filesystem. The only writable places are four size-limited tmpfs mounts, all
#                      nosuid and nodev: /tmp (noexec; HOME), /var/tmp (noexec, small), /dev/shm (noexec, small) and
#                      /work (TMPDIR: pytest's temporary directories and the test databases). /work allows exec because
#                      the install-script tests write stub programs (a fake curl) there and run them
#   --memory = --memory-swap   a memory cap with no swap; --pids-limit caps processes and threads
run_args=(--rm --timeout "${PENGUWATCH_TEST_TIMEOUT:-900}" --network none --cap-drop=ALL
          --security-opt no-new-privileges --read-only --tmpfs /tmp:rw,size=256m,nosuid,nodev,noexec
          --tmpfs /var/tmp:rw,size=16m,nosuid,nodev,noexec --tmpfs /dev/shm:rw,size=16m,nosuid,nodev,noexec
          --tmpfs /work:rw,size=1g,nosuid,nodev,exec
          --memory 2g --memory-swap 2g --pids-limit 512 -e HOME=/tmp -e TMPDIR=/work)
seed_mount=""
cmd=(python -m pytest -q -p no:cacheprovider)
if [ "${1:-}" = "--seed" ]; then
    shift
    [ -d "$ROOT/.seed" ] || { echo "no .seed/ library in $ROOT" >&2; exit 2; }
    seed_mount="$ROOT/.seed:/seed:ro,Z"  # read-only: the tests only read the library
    run_args+=(-e PENGUWATCH_SEED_DIR=/seed)
fi
if [ "${1:-}" = "--command" ]; then
    shift
    seed_mount="${seed_mount/:ro,Z/:rw,Z}"  # only an explicit command (the golden recorder) may write to .seed
    cmd=("$@")
    set --
fi

[ -z "$seed_mount" ] || run_args+=(-v "$seed_mount")

podman build -q --build-arg "APT_REFRESH=$(date -u +%Y-%m-%d)" -f "$ROOT/Dockerfile.test" -t "$IMAGE" "$ROOT" >/dev/null
exec podman run "${run_args[@]}" "$IMAGE" "${cmd[@]}" "$@"
