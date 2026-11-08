"""Opt-in browser smoke test of the dashboard (PENGUWATCH_UI_SMOKE=1).

Starts the real app on a free port with the made-up demo library (scripts/demo_library.py; the private .seed/ library is
never used), drives it with a headless browser through puppeteer-core (tests/ui/smoke.mjs) and fails on any page error,
console error, CSP violation or failed request. Supported way to run it: scripts/test-ui.sh, which builds Dockerfile.ui
(on the shared ui-test-runner image: Chromium, Firefox, Node, the npm packages pinned in tests/ui/runner/package-lock.json)
and runs the test in a locked-down container with no network and a read-only root filesystem. Alternative: any installed
Chrome named by PUPPETEER_EXECUTABLE_PATH, with puppeteer-core installed from the lockfile into the directory $PGBROWSER
(optionally PENGUWATCH_BROWSER_ARGS for extra Chrome flags). None of it is a dependency of the project, so the test is
skipped unless node and puppeteer-core exist.
"""

import asyncio
import importlib.util
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
UI = Path(__file__).resolve().parent / "ui"
PGBROWSER = os.environ.get("PGBROWSER") or str(Path.home() / "pgbrowser")
# A single-label name, like a home server's: the app accepts those by default; tests/ui/lib.mjs maps it to 127.0.0.1 in the browser.
BROWSER_HOST = "penguwatch-test"
TOKEN = "smoke-token-not-a-secret"  # made up for the test; the `token` variant signs in with it

pytestmark = pytest.mark.skipif(
    os.environ.get("PENGUWATCH_UI_SMOKE") != "1" or not shutil.which("node")
    or not (Path(PGBROWSER) / "node_modules" / "puppeteer-core").is_dir(),
    reason="run scripts/test-ui.sh, or set PENGUWATCH_UI_SMOKE=1 with node, $PGBROWSER (puppeteer-core) and PUPPETEER_EXECUTABLE_PATH")


def demo_library():
    spec = importlib.util.spec_from_file_location("demo_library", ROOT / "scripts" / "demo_library.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


# (variant, browser): `demo` is the made-up library with SIMKL not connected; `token` is the same behind an access token
# (the run signs in through the sign-in page first); `themed` is the same app with config.yaml display.theme: nord (the
# steps of core.mjs that say variants: ["themed"]); `pinned` is the same app with PENGUWATCH_TZ and PENGUWATCH_NOTIFY_URL set
# (settings.mjs: a setting pinned by the environment is read-only), PENGUWATCH_LOG_LEVEL invalid (ignored) and a config.yaml
# webhook that signs with PENGUWATCH_WEBHOOK_DEMO (read-only on the page); the Firefox run is one pass over the core steps only.
RUNS = [pytest.param("demo", "chromium", id="demo"), pytest.param("token", "chromium", id="token"),
        pytest.param("themed", "chromium", id="themed"), pytest.param("pinned", "chromium", id="pinned"),
        pytest.param("demo", "firefox", id="demo-firefox")]


@pytest.mark.parametrize("variant, browser", RUNS)
def test_dashboard_in_headless_browser(tmp_path, variant, browser):
    """`demo`: every panel and dialog on the demo library. `token`: the same app behind PENGUWATCH_ACCESS_TOKEN: the
    sign-in page, then the core steps. `themed`: the server's default theme (display.theme). `demo-firefox`: the core steps in Firefox (PENGUWATCH_BROWSER=firefox)."""
    db = tmp_path / "smoke.db"
    asyncio.run(demo_library().build(str(db)))
    config = tmp_path / "config.yaml"
    config.write_text(f'database: {{db_path: "{db.as_posix()}"}}\n' + ("display: {theme: nord}\n" if variant == "themed" else "")
                      # a 0.15 webhook that signs with a variable: imported once, shown read-only (settings.mjs)
                      + ("webhooks:\n  - {id: imported, name: Imported, url: 'https://192.0.2.30/in', secret_env: PENGUWATCH_WEBHOOK_DEMO}\n"
                         if variant == "pinned" else ""), encoding="utf-8")

    # Its own data directory for every run: the default is per user, shared by all runs in the container, and a shared
    # sessions.json lets one run affect another.
    env = {**os.environ, "PENGUWATCH_BROWSER": browser, "PYTHONPATH": str(ROOT / "src"), "PENGUWATCH_CONFIG": str(config),
           "PENGUWATCH_DATA": str(tmp_path / "data"), "PENGUWATCH_DB_PATH": str(db), "PENGUWATCH_HOST": "127.0.0.1",
           "PGBROWSER": PGBROWSER}
    for name in ("PENGUWATCH_ACCESS_TOKEN", "PENGUWATCH_ALLOWED_HOSTS", "PENGUWATCH_ALLOWED_ORIGINS", "PENGUWATCH_VIEW_WITHOUT_TOKEN"):
        env.pop(name, None)
    if variant == "token":
        env["PENGUWATCH_ACCESS_TOKEN"] = env["PENGUWATCH_SMOKE_TOKEN"] = TOKEN
    if variant == "pinned":  # made-up values: the notify address is a secret the page must never show
        env["PENGUWATCH_TZ"] = "Europe/Berlin"
        env["PENGUWATCH_NOTIFY_URL"] = "https://ntfy.example.com/topic-secret"
        env["PENGUWATCH_LOG_LEVEL"] = "loud"  # not a level: ignored, said on the Settings tab
        env["PENGUWATCH_WEBHOOK_DEMO"] = "made-up-demo-signing-secret"
    server = None
    try:
        # free_port() closes its socket before the app binds, and another process can take the port in between. Then the
        # app exits with "address already in use" and this starts again on another port.
        for attempt in range(5):
            port = free_port()
            env["PENGUWATCH_PORT"] = str(port)
            server = subprocess.Popen([sys.executable, "-c", "from penguwatch.main import main; main()"], cwd=tmp_path, env=env,
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
            # The browser opens the app by a name that is not local (lib.mjs maps it to 127.0.0.1), as people do over the LAN:
            # a page on 127.0.0.1 or localhost is a secure context, where the Clipboard API and notifications work, so it
            # hides what fails over http://homeserver:8080. The checks of this file talk to 127.0.0.1 directly.
            base = f"http://{BROWSER_HOST}:{port}"
            local = f"http://127.0.0.1:{port}"
            for _ in range(100):
                if server.poll() is not None:
                    break
                try:
                    if urllib.request.urlopen(f"{local}/api/health", timeout=1).status == 200:
                        break
                except OSError:
                    time.sleep(0.2)
            else:
                server.terminate()
                pytest.fail(f"the app did not start:\n{server.communicate(timeout=10)[0]}")
            if server.poll() is None:
                time.sleep(0.5)  # an answer may have come from another process's app that took this port first: ours exits
            if server.poll() is None:
                break
            output = server.communicate(timeout=10)[0]
            if "address already in use" not in output.lower() or attempt == 4:
                pytest.fail(f"the app did not start:\n{output}")
        out_dir = tmp_path / "out"  # screenshots; tmp_path is the only writable place in the container
        out_dir.mkdir()
        run = subprocess.run(["node", str(UI / "smoke.mjs"), base, str(out_dir), variant], env=env, capture_output=True, text=True, timeout=300)
        print(run.stdout)  # shown with -s: the steps that passed
        assert run.returncode == 0, f"{run.stdout}\n{run.stderr}"
    finally:
        if server is not None and server.poll() is None:
            server.terminate()
            try:
                server.wait(timeout=10)
            except subprocess.TimeoutExpired:
                server.kill()
