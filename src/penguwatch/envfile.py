"""penguwatch.env: settings for a native install that are not in config.yaml, mainly secrets.

A plain text file of KEY=value lines (blank lines and lines starting with # are ignored; a value may be wrapped in
matching single or double quotes; there is no variable expansion). It lives next to config.yaml (`penguwatch paths`
shows where), or wherever PENGUWATCH_ENV_FILE says. The `penguwatch` command loads it before it starts the server, so
the same file works for a terminal, a systemd service, a launchd job and the Windows logon task, and a secret never
has to be written into a service definition. A variable that is already in the environment wins over the file.

Example (see .env.example in the repository for the full list):

    PENGUWATCH_ACCESS_TOKEN=a-long-random-string
    PENGUWATCH_NOTIFY_URL=https://ntfy.example/penguwatch

Keep the file readable by you only; the installer creates it that way.
"""

import os
import re
from pathlib import Path
from typing import Dict, List, MutableMapping, Optional, Tuple

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
# What a settings file must not change: the program search path and the dynamic loader (nothing here needs them).
_FORBIDDEN_PREFIXES = ("LD_", "DYLD_", "PYTHON")
_FORBIDDEN = {"PATH"}

TEMPLATE = """\
# PenguWatch settings that are not in config.yaml. One KEY=value per line; a variable already set in the environment wins.
# Keep this file readable by you only. See docs/security.md and .env.example in the repository for the full list.

# Strongly recommended before PenguWatch listens beyond this computer (--host 0.0.0.0). Generate one with:
#   python -c "import secrets; print(secrets.token_urlsafe(32))"
# PENGUWATCH_ACCESS_TOKEN=

# Daily digest: an ntfy topic URL or a webhook that accepts JSON
# PENGUWATCH_NOTIFY_URL=
# PENGUWATCH_NOTIFY_KIND=ntfy
# PENGUWATCH_NOTIFY_HOUR=9

# Links to your Grafana in the page header
# PENGUWATCH_GRAFANA_URL=

# The secret named by `secret_env:` of an event webhook in config.yaml goes here as NAME=value
"""


def parse(text: str) -> Tuple[Dict[str, str], List[str]]:
    """(the variables, one message per line that was skipped). The messages name the line number, never a value."""
    values: Dict[str, str] = {}
    problems: List[str] = []
    for number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        name, sep, value = line.partition("=")
        name, value = name.strip(), value.strip()
        if not sep or not _NAME.fullmatch(name):
            problems.append(f"line {number}: expected NAME=value")
            continue
        if name in _FORBIDDEN or name.startswith(_FORBIDDEN_PREFIXES):
            problems.append(f"line {number}: {name} cannot be set from this file")
            continue
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if "\x00" in value:
            problems.append(f"line {number}: the value of {name} contains a NUL character")
            continue
        values[name] = value
    return values, problems


def load(path: Path, environ: Optional[MutableMapping[str, str]] = None) -> List[str]:
    """Puts the file's variables into `environ` (default os.environ) unless they are already there; returns the
    problems. A file that is missing or that this account cannot read (the system service's, which systemd has
    already loaded) is not a problem."""
    environ = os.environ if environ is None else environ
    try:
        text = path.read_text(encoding="utf-8")
    except (FileNotFoundError, PermissionError, NotADirectoryError):
        return []
    except (OSError, UnicodeDecodeError) as exc:
        return [f"cannot read {path}: {exc.__class__.__name__}"]
    values, problems = parse(text)
    for name, value in values.items():
        environ.setdefault(name, value)
    return [f"{path} {p}" for p in problems]
