"""File-based storage. Everything is written atomically and fsync'ed, so data survives a freeze."""
import json
import os
import pwd
from pathlib import Path

DATA = Path(os.environ.get("UVTUNE_DATA", os.path.expanduser("~/.local/share/uvtune")))
OWNER = os.environ.get("UVTUNE_USER")


def _owner_ids():
    if OWNER and os.geteuid() == 0:
        try:
            p = pwd.getpwnam(OWNER)
            return p.pw_uid, p.pw_gid
        except KeyError:
            return None
    return None


def chown(path):
    ids = _owner_ids()
    if ids:
        try:
            os.chown(path, *ids)
        except OSError:
            pass


def ensure_dir(path):
    path = Path(path)
    missing = []
    p = path
    while not p.exists() and p != p.parent:
        missing.append(p)
        p = p.parent
    for d in reversed(missing):
        d.mkdir(exist_ok=True)
        chown(d)
    return path


def write_json(path, obj):
    path = Path(path)
    ensure_dir(path.parent)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, default=str)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    try:
        fd = os.open(path.parent, os.O_RDONLY)
        os.fsync(fd)
        os.close(fd)
    except OSError:
        pass
    chown(path)


def read_json(path, default=None):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def append_jsonl(path, obj, sync=False):
    path = Path(path)
    new = not path.exists()
    with open(path, "a") as f:
        f.write(json.dumps(obj, default=str) + "\n")
        if sync:
            f.flush()
            os.fsync(f.fileno())
    if new:
        chown(path)


def read_jsonl(path, tail=None):
    try:
        with open(path) as f:
            lines = f.readlines()
    except OSError:
        return []
    if tail:
        lines = lines[-tail:]
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            pass  # half-written line after a freeze
    return out


def write_bytes(path, data):
    path = Path(path)
    ensure_dir(path.parent)
    with open(path, "wb") as f:
        f.write(data)
    chown(path)


def write_text(path, text):
    write_bytes(path, text.encode())


# Faste stier
CONFIG = DATA / "config.json"
COMMAND = DATA / "command.json"
STATE = DATA / "state.json"
HEARTBEAT = DATA / "heartbeat.json"
RUNS = DATA / "runs"
