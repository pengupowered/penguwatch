#!/usr/bin/env bash
# The runs go one after another (no pytest-xdist).
# Builds the UI test image (Dockerfile.ui: PenguWatch's Python setup on top of the UI test runner image built by
# tests/ui/runner/build.sh, which has Chromium, Firefox, Node, puppeteer-core, ESLint, all pinned) and runs the browser
# tests in a locked-down throwaway container. Nothing is installed on the host. Extra args go to pytest.
#   scripts/test-ui.sh                   all three UI test files
#   scripts/test-ui.sh -k lint -x        pytest arguments
#   scripts/test-ui.sh --command <cmd...>  run this command in the container instead of pytest
# PENGUWATCH_JUNIT_DIR=<dir> (CI): pytest also writes <dir>/ui.xml; the one writable mount.
#   PENGUWATCH_TEST_MEMORY=<size>   memory cap, no swap (default 3g: the app, Node and a browser, and /tmp is a tmpfs).
#   PENGUWATCH_TEST_TIMEOUT=<s>     podman kills the container after this long (default 900).
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BASE="${UI_RUNNER_BASE:-localhost/ui-test-runner:1}"

# One browser run may take 300 s (test_ui_smoke.py), so the hang guard allows 360 here.
cmd=(python -m pytest -q -p no:cacheprovider -o faulthandler_timeout=360 tests/test_ui_smoke.py tests/test_ui_lint.py tests/test_ui_steps.py)
mounts=()
if [ "${1:-}" = "--command" ]; then
    shift
    cmd=("$@")
    set --
elif [ -n "${PENGUWATCH_JUNIT_DIR:-}" ]; then
    mounts=(-v "$PENGUWATCH_JUNIT_DIR:/junit:rw")
    cmd+=(--junitxml=/junit/ui.xml -o junit_family=xunit1)
fi

if ! podman image exists "$BASE"; then
    echo "base image $BASE is missing: run tests/ui/runner/build.sh first" >&2
    exit 1
fi

# The only step with network access (pip, for the hash-locked Python test requirements).
# Run by id, not by a tag, so two runs at once never test each other's code. The image is tagged per work tree and the
# work tree's previous image is removed after the build (scripts/lib-test-image.sh): no dangling images are left.
. "$ROOT/scripts/lib-test-image.sh"
build_test_image penguwatch-ui-test "$ROOT" Dockerfile.ui --build-arg "BASE=$BASE"

memory="${PENGUWATCH_TEST_MEMORY:-3g}"

# The hardening flags:
# --rm                          throwaway container.
# --network none                the tests only use loopback inside the container; no route out.
# --cap-drop=ALL                no Linux capabilities (the image's user is non-root anyway).
# --security-opt no-new-privileges   no privilege gain through setuid binaries.
# --read-only                   root filesystem is immutable; only the tmpfs below is writable.
# --tmpfs /tmp:...              the one writable place (pytest tmp dirs, seeded DB, data dirs, screenshots, the browser's
#                               profile); size-limited, no setuid/devices, not executable.
# --memory, --memory-swap       caps runaway browsers; swap equal to memory means no swap.
# --pids-limit                  caps processes/threads (browsers fork many).
# --shm-size                    /dev/shm for the browser's renderer; the 64m default is tight.
# --timeout                     podman itself kills the container after this long, also when this script was killed
#                               (a killed `podman run` leaves its container running otherwise).
exec podman run --rm \
    --timeout "${PENGUWATCH_TEST_TIMEOUT:-900}" \
    --network none \
    --cap-drop=ALL \
    --security-opt no-new-privileges \
    --read-only \
    --tmpfs /tmp:rw,size=1g,nosuid,nodev,noexec \
    --memory "$memory" --memory-swap "$memory" \
    --pids-limit 1024 \
    --shm-size 512m \
    "${mounts[@]}" \
    "$built" "${cmd[@]}" "$@"
