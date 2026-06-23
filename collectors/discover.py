"""
Discover all .claude* directories on the machine and build a developer identity map.

Output schema:
  {
    "developer_key": str,       # SHA-256(email) or SHA-256(hostname) — stable ID
    "name":          str | None,
    "email":         str | None,
    "claude_dirs":   [str, ...] # all .claude* dirs belonging to this developer
  }
"""

import hashlib
import json
import logging
import os
import socket
import subprocess
from pathlib import Path

logger = logging.getLogger(__name__)


def _sha256(value: str) -> str:
    return hashlib.sha256(value.strip().encode()).hexdigest()


def _git_identity(cwd: str | None = None) -> tuple[str | None, str | None]:
    """Return (name, email) from git config in cwd, or global git config."""
    name = email = None
    for field, key in [("name", "user.name"), ("email", "user.email")]:
        try:
            val = subprocess.check_output(
                ["git", "config", key],
                cwd=cwd or ".",
                text=True,
                stderr=subprocess.DEVNULL,
                timeout=2,
            ).strip()
            if field == "name":
                name = val or None
            else:
                email = val or None
        except Exception:
            pass
    return name, email


def _decode_project_path(dir_name: str) -> str | None:
    """
    Reconstruct a filesystem path from Claude Code's encoded project directory name.

    Linux/macOS: -home-kalpaj-Documents-foo  → /home/kalpaj/Documents/foo
    Windows    : -C--Users-kalpaj-foo        → C:\\Users\\kalpaj\\foo
                 (Claude encodes C:\\ as -C- and \\ as -)
    """
    if not dir_name.startswith("-"):
        return None

    # Windows: encoded path starts with a drive letter pattern like -C- or -D-
    import re
    win_match = re.match(r"^-([A-Za-z])-(.+)$", dir_name)
    if win_match and os.name == "nt":
        drive = win_match.group(1).upper()
        rest  = win_match.group(2).replace("-", "\\")
        return f"{drive}:\\{rest}"

    # Unix: leading - becomes /, remaining - become /
    return "/" + dir_name[1:].replace("-", "/")


def _read_claude_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text())
    except Exception:
        return {}


def _developer_key_from_dir(claude_dir: Path) -> tuple[str, str | None, str | None]:
    """
    Derive developer_key for a given .claude* directory.
    Priority: git config email → hostname fallback.
    Returns (developer_key, name, email).
    """
    # Try git identity from within any project directory in this claude dir
    projects_dir = claude_dir / "projects"
    name = email = None
    if projects_dir.exists():
        for project_subdir in projects_dir.iterdir():
            if project_subdir.is_dir():
                candidate = _decode_project_path(project_subdir.name)
                n, e = _git_identity(candidate if candidate and Path(candidate).exists() else None)
                if e:
                    name, email = n, e
                    break

    if not email:
        name, email = _git_identity()

    if email:
        return _sha256(email), name, email

    hostname = socket.gethostname()
    return _sha256(hostname + str(claude_dir)), name, None


def find_claude_dirs(home: Path | None = None) -> list[Path]:
    """Return all claude data directories for the current user (cross-platform)."""
    base = home or Path.home()
    dirs = []

    # Linux / macOS: ~/.claude, ~/.claude-work, ~/.claude-personal, etc.
    for entry in base.iterdir():
        if entry.name.startswith(".claude") and entry.is_dir():
            dirs.append(entry)

    # Windows: %APPDATA%\Claude  (Electron default for Claude Code)
    if os.name == "nt":
        appdata = os.environ.get("APPDATA")
        if appdata:
            win_dir = Path(appdata) / "Claude"
            if win_dir.is_dir() and win_dir not in dirs:
                dirs.append(win_dir)

    return sorted(dirs)


def build_developer_map(home: Path | None = None) -> list[dict]:
    """
    Build a list of developer identity records, one per unique developer_key.
    Merges multiple .claude* dirs that belong to the same developer.
    """
    claude_dirs = find_claude_dirs(home)
    grouped: dict[str, dict] = {}

    for d in claude_dirs:
        key, name, email = _developer_key_from_dir(d)
        if key not in grouped:
            grouped[key] = {
                "developer_key": key,
                "name": name,
                "email": email,
                "claude_dirs": [],
            }
        grouped[key]["claude_dirs"].append(str(d))
        # Fill in name/email if missing
        if not grouped[key]["name"] and name:
            grouped[key]["name"] = name
        if not grouped[key]["email"] and email:
            grouped[key]["email"] = email

    return list(grouped.values())


if __name__ == "__main__":
    import json as _json
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    for dev in build_developer_map():
        logger.info(_json.dumps(dev, indent=2))
