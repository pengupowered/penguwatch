#!/usr/bin/env bash
# Builds and (re)starts PenguWatch on a Linux host with rootless Podman (docker-compose.yml).
#
#   scripts/deploy.sh              gate, backup, build, recreate, wait until healthy
#   scripts/deploy.sh --no-build   same, reusing localhost/penguwatch:latest
#   scripts/deploy.sh --gate-only  run only the dependency gate (exit 0 if it passed)
#   scripts/deploy.sh --dry-run    run the gate, then print the steps instead of running them
#
# The dependency gate always runs first, in every mode, and has no skip switch: `depguard.sh check` (are the
# requirements*.txt locks current?) and `depguard.sh audit` (advisories, cooldown, yanked releases) from
# $DEPGUARD_HOME (default ~/src/depguard; depguard is an optional external tool, see docs/supply-chain.md, but
# this script is written to require it). A missing depguard is a failure. Exceptions are made only through
# .depguard-ignore.yaml (see the depguard README). Without depguard, use docker-compose.yml directly (README).
#
# Before replacing a running container it keeps one rollback point (older ones are removed):
#   * the current image, tagged localhost/penguwatch:pre-deploy-<stamp>
#   * ~/backups/penguwatch/<stamp>/ with a consistent copy of the database (VACUUM INTO; it holds the
#     SIMKL token, so the folder is private to this user)
# Rollback: podman tag localhost/penguwatch:pre-deploy-<stamp> localhost/penguwatch:latest, put the backup
# database back into the volume, then scripts/deploy.sh --no-build.
#
# Host setup this relies on (once): loginctl enable-linger and systemctl --user enable podman-restart.service.
set -u

NAME="penguwatch"
IMAGE="localhost/penguwatch:latest"
PORT=8080
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
DEPGUARD_HOME="${DEPGUARD_HOME:-$HOME/src/depguard}"
BACKUPS="${PENGUWATCH_BACKUPS:-$HOME/backups/penguwatch}"
STAMP="$(date +%Y-%m-%d-%H%M)"

NO_BUILD=0; GATE_ONLY=0; DRY_RUN=0
for a in "$@"; do
    case "$a" in
        --no-build) NO_BUILD=1 ;;
        --gate-only) GATE_ONLY=1 ;;
        --dry-run) DRY_RUN=1 ;;
        -h|--help) sed -n '2,22p' "${BASH_SOURCE[0]}"; exit 0 ;;
        *) echo "unknown option: $a" >&2; exit 2 ;;
    esac
done

step() { printf '\033[36m==> %s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*" >&2; exit 1; }
run() {  # prints instead of running in a dry run
    if [ $DRY_RUN = 1 ]; then printf '\033[33mDRYRUN: %s\033[0m\n' "$*"; else "$@"; fi
}

cd "$ROOT" || exit 2
# docker-compose.yml mounts ./config.yaml; a missing file would be created as an empty directory
[ -f config.yaml ] || fail "No config.yaml: copy config.example.yaml to config.yaml and edit it first."

# ---- dependency gate (always) ----
if [ ! -x "$DEPGUARD_HOME/depguard.sh" ]; then
    fail "Dependency gate unavailable: $DEPGUARD_HOME/depguard.sh not found (set DEPGUARD_HOME or clone depguard there). Refusing to deploy."
fi
for s in check audit; do
    step "Dependency gate: depguard $s"
    "$DEPGUARD_HOME/depguard.sh" "$s" "$ROOT"
    code=$?
    [ $code -eq 0 ] || fail "Dependency gate failed at 'depguard $s' (exit $code); deploy aborted. Fix the lock/advisory (re-lock with depguard.sh lock) or record a time-limited exception in $ROOT/.depguard-ignore.yaml."
done
printf '\033[32m==> Dependency gate passed\033[0m\n'
[ $GATE_ONLY = 1 ] && exit 0

# ---- rollback point ----
running=0
podman container exists "$NAME" 2>/dev/null && [ "$(podman inspect -f '{{.State.Running}}' "$NAME")" = true ] && running=1
if podman image exists "$IMAGE" 2>/dev/null; then
    step "Tagging the current image as pre-deploy-$STAMP"
    run podman tag "$IMAGE" "localhost/penguwatch:pre-deploy-$STAMP" || fail "tagging the rollback image failed"
fi
if [ $running = 1 ]; then
    step "Backing up the database to $BACKUPS/$STAMP"
    run mkdir -p "$BACKUPS/$STAMP"
    run podman exec "$NAME" python -c "import sqlite3; c = sqlite3.connect('data/penguwatch.db'); c.execute(\"VACUUM INTO 'data/.pre-deploy.db'\"); c.close()" \
        || fail "database backup failed; nothing was changed"
    run podman cp "$NAME:/app/data/.pre-deploy.db" "$BACKUPS/$STAMP/penguwatch.db" || fail "copying the backup out failed"
    run podman exec "$NAME" rm -f data/.pre-deploy.db
    run chmod -R go-rwx "$BACKUPS/$STAMP"
fi

# ---- build and recreate ----
if [ $NO_BUILD = 0 ]; then
    step "Building $IMAGE"
    run podman-compose build || fail "build failed"
fi
# down sends SIGTERM, so pending SIMKL changes are flushed before the old container goes
step "Recreating the container"
run podman-compose down || fail "removing the old container failed"
run podman-compose up -d || fail "starting the container failed"

if [ $DRY_RUN = 1 ]; then step "Dry run finished: nothing was changed"; exit 0; fi

# /api/health answers {"status": "ok"} once the app is ready; the first start after an upgrade may migrate
# the database first.
step "Waiting for the API (up to 2 minutes)"
for _ in $(seq 1 40); do
    if curl -fsS "http://127.0.0.1:$PORT/api/health" 2>/dev/null | jq -e '.status == "ok"' >/dev/null 2>&1; then
        ver="$(curl -fsS "http://127.0.0.1:$PORT/openapi.json" 2>/dev/null | jq -r '.info.version' 2>/dev/null)"
        printf '\033[32mPenguWatch %s is up: http://%s:%s\033[0m\n' "$ver" "$(hostname -I | awk '{print $1}')" "$PORT"
        # one rollback point per project: drop the older ones, but only once this run made a new one
        if podman image exists "localhost/penguwatch:pre-deploy-$STAMP"; then
            podman images --format '{{.Repository}}:{{.Tag}}' | grep '^localhost/penguwatch:pre-deploy-' \
                | grep -v ":pre-deploy-$STAMP\$" | xargs -r podman rmi >/dev/null 2>&1
        fi
        if [ -f "$BACKUPS/$STAMP/penguwatch.db" ]; then
            find "$BACKUPS" -mindepth 1 -maxdepth 1 -type d ! -name "$STAMP" -exec rm -rf {} + 2>/dev/null
        fi
        exit 0
    fi
    if [ "$(podman inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" != true ]; then
        podman logs --tail 40 "$NAME" 2>&1
        fail "PenguWatch isn't running; the rollback image and backup were kept"
    fi
    sleep 3
done
podman logs --tail 40 "$NAME" 2>&1
fail "PenguWatch didn't become healthy within 2 minutes; the rollback image and backup were kept"
