"""secrets.json: the Settings tab's write-only secrets, next to the database (penguwatch.paths.secrets_path).

    {"version": 1,
     "notify": {"url": "https://ntfy.example.com/topic"},
     "webhooks": {"home-assistant": {"url": "https://192.0.2.10/hook", "secret": "0123456789abcdef"}}}

A *slot* is a named place in the file (penguwatch.settings.registry.SECRET_SLOTS): `notify.url` (the daily digest's
address; PENGUWATCH_NOTIFY_URL wins while it is set), `webhook.<id>.url` and `webhook.<id>.secret` (a webhook's
address and stored signing secret; the item's `secret_env` / `secret_file` win for the secret). The page writes a
slot and never reads it back: an answer says only whether it is set, where from and, for an address, its host.

The file is read once and kept (SecretStore), written atomically (a temp file with mode 0600, fsync, os.replace) and
never written when the file on disk cannot be read (a write would erase what it holds). Nothing here logs a value: a
problem is logged with the path and what is wrong, never the content. The drivers' logins stay in the database
(app_state); this file holds only the Settings tab's secrets.
"""

import json
import logging
import os
import stat
import sys
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

_log = logging.getLogger("penguwatch.secrets")

FILE_VERSION = 1
SECRET_FILE_MAX_BYTES = 4096  # a secret file (secret_file) larger than this is a mistake (a key, a log), not a secret

# What the page says when a save is refused for that reason (the log line names the path and the problem).
SECRETS_FILE_DAMAGED = ("The secrets file (secrets.json) is damaged, so it was not overwritten and nothing was saved. "
                        "Fix the file, or move it away to start over, then save again.")


class SecretsFileUnusable(OSError):
    """secrets.json exists but cannot be read or parsed, so a write would erase what it holds: nothing was written.
    An OSError, so every caller that handles a failed write refuses the save; the message never holds the content."""

    def __init__(self, path: Path, problem: str):
        super().__init__(f"{path} {problem}; it was not overwritten")
        self.path, self.problem = path, problem


def _text(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _read_store(path: Path) -> Tuple[Dict, Optional[str]]:
    """(secrets.json parsed, None), or ({}, problem) when the file exists but cannot be read or parsed; ({}, None) when
    it is missing. Problems are logged with the path, never the content."""
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}, None
    except (OSError, UnicodeDecodeError) as e:
        _log.warning(f"Secrets file {path} could not be read ({type(e).__name__}); the secrets in it are not used")
        return {}, "could not be read"
    try:
        data = json.loads(raw)
    except ValueError as e:
        _log.warning(f"Secrets file {path} is not valid JSON ({type(e).__name__} at line {getattr(e, 'lineno', '?')}); "
                     "the secrets in it are not used")
        return {}, "is not valid JSON"
    if not isinstance(data, dict):
        _log.warning(f"Secrets file {path} is not a JSON object; the secrets in it are not used")
        return {}, "is not a JSON object"
    try:
        # Windows has no permission bits (st_mode is 0o666 or 0o444); the profile's ACL protects the file there
        mode = stat.S_IMODE(path.stat().st_mode) if sys.platform != "win32" else 0
    except OSError:
        mode = 0
    if mode & 0o077:
        _log.warning(f"Secrets file {path} is readable by other users (mode {mode:04o}); PenguWatch writes it 0600")
    return data, None


def _read_for_write(path: Path) -> Dict:
    """secrets.json as it is on disk now, to merge a change into ({} when missing). Raises SecretsFileUnusable when the
    file exists but cannot be read or parsed."""
    data, problem = _read_store(path)
    if problem is not None:
        _log.error(f"Secrets file {path} {problem}: nothing was saved, so the secrets in it are kept. Fix the file (or "
                   "move it away to start over), then save again")
        raise SecretsFileUnusable(path, problem)
    return data


def _merge(into: dict, updates: dict) -> None:
    """Merges `updates` into `into`, recursing into mappings present in both; any other value is replaced."""
    for key, value in updates.items():
        if isinstance(value, dict) and isinstance(into.get(key), dict):
            _merge(into[key], value)
        else:
            into[key] = value


def _remove_path(data: dict, path: Tuple[str, ...]) -> None:
    """Deletes data[path...]; a mapping left empty by it is removed too."""
    if not path:
        return
    if len(path) == 1:
        data.pop(path[0], None)
        return
    parent = data.get(path[0])
    if isinstance(parent, dict):
        _remove_path(parent, path[1:])
        if not parent:
            data.pop(path[0], None)


def _copy(data: dict) -> dict:
    return json.loads(json.dumps(data))


def write_secrets(data: dict, path: Path) -> None:
    """Replaces secrets.json with `data`: written to a temp file with mode 0600, flushed to disk and moved into place,
    so a crash never leaves a half-written file and the secrets are never readable by others."""
    path = Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_BINARY", 0), 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, 0o600)  # O_CREAT's mode is ignored when a stale temp file existed
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            tmp.unlink()


class SecretStore:
    """secrets.json, read once (then kept) and changed in one atomic write per save. One instance per app (the
    settings component makes it), so every reader and writer uses the same path; tests pass a path or `data`."""

    def __init__(self, path: Path, *, data: Optional[dict] = None):
        self.path = Path(path)
        self._data = data
        self._problem: Optional[str] = None

    def data(self) -> dict:
        """The parsed file, read on first use; {} when it is missing or unusable (logged, without its content)."""
        if self._data is None:
            self._data, self._problem = _read_store(self.path)
        return self._data

    @property
    def problem(self) -> Optional[str]:
        """Why the file could not be used when it was read ("is not valid JSON"), else None (also for a missing
        file)."""
        self.data()
        return self._problem

    def get(self, path: Tuple[str, ...]) -> Optional[str]:
        """The text at `path` (("notify", "url")), or None when it is missing, empty or not text. Never log it."""
        node: object = self.data()
        for part in path:
            node = node.get(part) if isinstance(node, dict) else None
        return _text(node)

    def snapshot(self) -> dict:
        """A copy of what the store holds now (to put back after a failed save)."""
        return _copy(self.data())

    def change(self, updates: dict, removals: Sequence[Tuple[str, ...]] = ()) -> None:
        """Merges `updates` and removes `removals` (paths; a parent left empty goes too) in one atomic write, then
        updates what data() returns. Nothing is written when neither changes anything, nor when the file on disk
        cannot be read or parsed (SecretsFileUnusable)."""
        memory = _copy(self.data())
        _merge(memory, _copy(updates))
        for path in removals:
            _remove_path(memory, tuple(path))
        if memory == self.data():
            return
        on_disk = _read_for_write(self.path)
        _merge(on_disk, _copy(updates))
        for path in removals:
            _remove_path(on_disk, tuple(path))
        on_disk.setdefault("version", FILE_VERSION)
        write_secrets(on_disk, self.path)
        memory.setdefault("version", FILE_VERSION)
        self._data = memory

    def restore(self, previous: dict) -> None:
        """Writes `previous` (a snapshot) back as the whole file: after the database refused a save whose secrets
        were already written. Raises OSError when that fails too (the caller logs it)."""
        write_secrets(_copy(previous), self.path)
        self._data = _copy(previous)


# --- the slots ---------------------------------------------------------------------------------------------------------

def _webhook_id_ok(value: str) -> bool:
    from penguwatch.config import WEBHOOK_ID_RE
    return bool(WEBHOOK_ID_RE.fullmatch(value))


def slot_path(slot: str) -> Optional[Tuple[str, ...]]:
    """Where a slot lives in secrets.json, or None for a name that is no slot."""
    if slot == "notify.url":
        return ("notify", "url")
    if slot.startswith("webhook."):
        hook, _, part = slot[len("webhook."):].rpartition(".")
        if part in ("url", "secret") and _webhook_id_ok(hook):
            return ("webhooks", hook, part)
    return None


def slot_updates(secrets: Mapping[str, str]) -> Tuple[dict, List[Tuple[str, ...]]]:
    """(updates to merge, paths to remove) for {slot: value}; "" removes the slot. KeyError for a name that is no
    slot."""
    updates: dict = {}
    removals: List[Tuple[str, ...]] = []
    for slot, value in secrets.items():
        path = slot_path(slot)
        if path is None:
            raise KeyError(slot)
        if value:
            node = updates
            for part in path[:-1]:
                node = node.setdefault(part, {})
            node[path[-1]] = value
        else:
            removals.append(path)
    return updates, removals


def read_secret_file(path: Path) -> Optional[str]:
    """The text of a regular file of at most SECRET_FILE_MAX_BYTES (one trailing newline removed), or None. Never
    logs the content."""
    try:
        info = Path(path).stat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > SECRET_FILE_MAX_BYTES:
            return None
        text = Path(path).read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None
    text = text[:-2] if text.endswith("\r\n") else text[:-1] if text.endswith("\n") else text
    return text or None
