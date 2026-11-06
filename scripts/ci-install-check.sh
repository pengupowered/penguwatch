#!/usr/bin/env bash
# What the CI job "install" checks around the install scripts (.github/workflows/ci.yml); runs in bash on Linux,
# macOS and Windows (Git Bash). RUNNER_OS says which.
#   scripts/ci-install-check.sh installed <assets-dir>    after the first install: running, answering, who it runs as
#   scripts/ci-install-check.sh token                     the settings file works: with an access token the API refuses
#   scripts/ci-install-check.sh upgraded <assets-dir>     after running the installer again: backup made, answering
#   scripts/ci-install-check.sh removed                   after the uninstall: nothing answers, nothing is left
#   scripts/ci-install-check.sh diagnose                  on failure: service state and log tail as one error annotation
set -uo pipefail
phase="${1:?usage: ci-install-check.sh installed|token|upgraded|removed|diagnose [assets-dir]}"
assets="${2:-}"
port=8080
fails=0

case "${RUNNER_OS:-$(uname -s)}" in
    Linux)   pg=/opt/penguwatch/venv/bin/penguwatch; data=/var/lib/penguwatch; env_file=/etc/penguwatch/penguwatch.env; sudo=sudo ;;
    macOS|Darwin) pg=/usr/local/penguwatch/venv/bin/penguwatch; data="/Library/Application Support/PenguWatch"; env_file="$data/penguwatch.env"; sudo=sudo ;;
    *)       local_app="$(cygpath -u "$LOCALAPPDATA")"; pg="$local_app/Programs/PenguWatch/venv/Scripts/penguwatch.exe"; data="$local_app/PenguWatch"
             env_file="$(cygpath -u "$APPDATA")/PenguWatch/penguwatch.env"; sudo= ;;
esac
os="${RUNNER_OS:-$(uname -s)}"

ok() { echo "ok: $*"; }
# A failed check is also an error annotation (the annotation is what can be read without signing in to GitHub).
# `bad WHAT [OUTPUT]`: the output's last line, which for a Python traceback is the exception, comes first.
bad() {
    echo "FAILED: $1" >&2; fails=$((fails + 1))
    local detail="${2:-}" last
    if [ -n "$detail" ]; then
        last="$(printf '%s\n' "$detail" | grep -v '^[[:space:]]*$' | tail -n 1)"
        echo "$detail" | tail -n 40 >&2
        annotate "install check ($os)" "$1: $last"$'\n'"$(printf '%s\n' "$detail" | tail -n 25)"
    else
        annotate "install check ($os)" "$1"
    fi
}
check() {
    local what="$1" out; shift
    if out="$("$@" 2>&1)"; then ok "$what"; else bad "$what" "$out"; fi
}

version_of_assets() { basename "$assets"/penguwatch-*-py3-none-any.whl | sed -n 's/^penguwatch-\([0-9][^-]*\)-.*/\1/p'; }
health() { curl -sf -m 5 "http://127.0.0.1:$port/api/health"; }
code_of() { curl -s -o /dev/null -m 5 -w '%{http_code}' "$1"; }
pid_of_service() { pgrep -f 'penguwatchw? serve|penguwatchw?\.exe serve' | head -n 1; }

annotate() {  # annotate TITLE TEXT: one GitHub error annotation (newlines and percent signs escaped)
    local text="${2//'%'/'%25'}"; text="${text//$'\r'/}"; text="${text//$'\n'/'%0A'}"
    echo "::error title=$1::${text:0:3500}"
}

wait_for_health() { for _ in $(seq 1 60); do health > /dev/null 2>&1 && return 0; sleep 2; done; return 1; }

case "$phase" in
installed|upgraded)
    want="$(version_of_assets)"
    wait_for_health; body="$(health)" && ok "/api/health answers: $body" || bad "/api/health does not answer"
    case "$body" in *'"status":"ok"'*) ok "it reports status ok" ;; *) bad "health does not say status ok: $body" ;; esac
    case "$("$pg" --version 2>&1)" in *"$want"*) ok "the installed program is version $want" ;; *) bad "the installed program is not version $want: $("$pg" --version 2>&1)" ;; esac
    check "penguwatch service status exits 0" "$pg" service status
    check "penguwatch doctor --offline finds nothing wrong" "$pg" doctor --offline
    check "the home page is served" curl -sf -m 10 -o /dev/null "http://127.0.0.1:$port/"
    case "$os" in
    Linux)
        check "systemd says the unit is active" systemctl is-active --quiet penguwatch.service
        user="$(ps -o user= -p "$(systemctl show -p MainPID --value penguwatch.service)" | tr -d ' ')"
        [ -n "$user" ] && [ "$user" != root ] && ok "it runs as $user, not root" || bad "it runs as '$user'"
        caps="$(grep '^CapEff' "/proc/$(systemctl show -p MainPID --value penguwatch.service)/status" | awk '{print $2}')"
        [ "$caps" = 0000000000000000 ] && ok "it has no capabilities" || bad "its effective capabilities are '$caps'"
        score_line="$(systemd-analyze security penguwatch.service --no-pager 2>&1 | grep 'Overall exposure level')"
        echo "$score_line"
        score="$(echo "$score_line" | sed -n 's/.*: \([0-9.]*\) .*/\1/p')"
        awk -v s="${score:-99}" 'BEGIN { exit !(s <= 1.5) }' && ok "exposure score $score is at most 1.5" || bad "exposure score '$score' is above 1.5"
        check "data folder has the database" $sudo test -f "$data/penguwatch.db"
        check "the settings file is private to root" test "$(stat -c %a "$env_file")" = 600
        ;;
    macOS|Darwin)
        user="$(ps -o user= -p "$(pid_of_service)" | tr -d ' ')"
        [ "$user" = _penguwatch ] && ok "it runs as _penguwatch" || bad "it runs as '$user'"
        check "data folder has the database" $sudo test -f "$data/penguwatch.db"
        check "the settings file is private" test "$($sudo stat -f %Lp "$env_file")" = 600
        ;;
    *)
        check "the task exists" schtasks //Query //TN PenguWatch
        check "data folder has the database" test -f "$data/penguwatch.db"
        check "the settings file exists" test -f "$env_file"
        ;;
    esac
    if [ "$phase" = upgraded ]; then
        count="$($sudo ls "$data/backups" 2> /dev/null | wc -l | tr -d ' ')"
        [ "${count:-0}" -ge 1 ] && ok "the upgrade made a backup ($count)" || bad "no backup in $data/backups"
    fi
    ;;
token)
    # The settings file is how a service gets its secrets: put a token in it, restart, and the API must refuse
    # requests without it while the health check stays open.
    token="ci-$(date +%s)-token-$$"
    case "$os" in
    Linux)
        printf 'PENGUWATCH_ACCESS_TOKEN=%s\n' "$token" | sudo tee "$env_file" > /dev/null
        sudo systemctl restart penguwatch.service ;;
    macOS|Darwin)
        printf 'PENGUWATCH_ACCESS_TOKEN=%s\n' "$token" | sudo -u _penguwatch tee "$env_file" > /dev/null
        sudo launchctl kickstart -k system/com.pengupowered.penguwatch ;;
    *)
        printf 'PENGUWATCH_ACCESS_TOKEN=%s\n' "$token" > "$env_file"
        schtasks //End //TN PenguWatch > /dev/null 2>&1; taskkill //F //T //IM penguwatchw.exe > /dev/null 2>&1
        schtasks //Run //TN PenguWatch > /dev/null ;;
    esac
    sleep 3
    wait_for_health && ok "/api/health stays open" || bad "/api/health does not answer after the restart"
    code="$(code_of "http://127.0.0.1:$port/api/stats")"
    [ "$code" = 401 ] && ok "the API refuses a request without the token ($code)" || bad "the API answered $code without the token"
    code="$(curl -s -o /dev/null -m 5 -w '%{http_code}' -H "Authorization: Bearer $token" "http://127.0.0.1:$port/api/stats")"
    [ "$code" = 200 ] && ok "the API accepts the token ($code)" || bad "the API answered $code with the token"
    case "$os" in
    Linux) printf '' | sudo tee "$env_file" > /dev/null ;;
    macOS|Darwin) printf '' | sudo -u _penguwatch tee "$env_file" > /dev/null ;;
    *) printf '' > "$env_file" ;;
    esac
    ;;
removed)
    ! health > /dev/null 2>&1 && ok "nothing answers on port $port" || bad "something still answers on port $port"
    [ ! -e "$pg" ] && ok "the program is gone" || bad "$pg is still there"
    case "$os" in
    Linux) [ ! -e /etc/systemd/system/penguwatch.service ] && ok "the unit is gone" || bad "the unit file is still there" ;;
    macOS|Darwin) [ ! -e /Library/LaunchDaemons/com.pengupowered.penguwatch.plist ] && ok "the plist is gone" || bad "the plist is still there" ;;
    *) ! schtasks //Query //TN PenguWatch > /dev/null 2>&1 && ok "the task is gone" || bad "the task is still there" ;;
    esac
    # /var/lib/penguwatch is a link into /var/lib/private, which only root can open
    if $sudo test -d "$data"; then ok "the data is kept"; else bad "the data directory was removed"; fi
    if $sudo test -f "$data/penguwatch.db"; then ok "the database is kept"; else bad "the database was removed"; fi
    ;;
diagnose)
    {
        echo "== service status"
        if [ -e "$pg" ]; then "$pg" service status 2>&1 | tail -n 40; else echo "$pg does not exist"; fi
        case "$os" in
        Linux) echo "== journalctl"; sudo journalctl -u penguwatch.service --no-pager -n 30 2>&1 ;;
        macOS|Darwin) echo "== log"; $sudo tail -n 30 "$data/penguwatch.log" 2>&1; $sudo launchctl print system/com.pengupowered.penguwatch 2>&1 | head -n 25 ;;
        *) echo "== log"; tail -n 30 "$data/penguwatch.log" 2>&1; schtasks //Query //TN PenguWatch //V //FO LIST 2>&1 | head -n 25 ;;
        esac
    } > "${RUNNER_TEMP:-/tmp}/install-diagnose.txt" 2>&1
    cat "${RUNNER_TEMP:-/tmp}/install-diagnose.txt"
    # the last exception line of any traceback first, then the log
    headline="$(grep -E '^[A-Za-z_.]+(Error|Exception|Exit)\b' "${RUNNER_TEMP:-/tmp}/install-diagnose.txt" | tail -n 1)"
    annotate "install ($os)" "${headline:+$headline$'\n'}$(cat "${RUNNER_TEMP:-/tmp}/install-diagnose.txt")"
    exit 0
    ;;
*) echo "unknown phase $phase" >&2; exit 2 ;;
esac
exit $((fails > 0))
