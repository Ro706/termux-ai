"""File tools the agent can request. Reads are free (minus secrets); writes need approval."""
from __future__ import annotations

import difflib
import fnmatch
import os
import re
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .config import DATA_DIR
from .security import SENSITIVE_PATH_PATTERNS, Verdict, redact

MAX_READ_BYTES = 60_000
RC_FILES = {".bashrc", ".bash_profile", ".profile", ".zshrc", ".zprofile", ".bash_login"}


class ToolError(Exception):
    pass


def _expand(path: str, cwd: str) -> Path:
    p = Path(os.path.expanduser(path))
    if not p.is_absolute():
        p = Path(cwd) / p
    return Path(os.path.abspath(p))


def is_sensitive(path: Path | str) -> bool:
    s = str(path)
    return any(re.search(pat, s) for pat in SENSITIVE_PATH_PATTERNS)


def _writable_roots() -> list[Path]:
    roots = [Path(os.path.expanduser("~")).resolve()]
    for extra in ("/sdcard", "/storage/emulated/0"):
        if os.path.isdir(extra):
            roots.append(Path(extra))
    tmp = os.environ.get("TMPDIR")
    if tmp:
        roots.append(Path(tmp).resolve())
    return roots


def _within(path: Path, roots: list[Path]) -> bool:
    real = path.resolve()
    return any(real == r or r in real.parents for r in roots)


# ------------------------------------------------------------------- read
def read_file(path: str, cwd: str, max_bytes: int = MAX_READ_BYTES) -> str:
    p = _expand(path, cwd)
    if is_sensitive(p):
        raise ToolError(f"refusing to read {p.name}: looks like a credential/secret file")
    if not p.is_file():
        raise ToolError(f"not a file: {p}")
    size = p.stat().st_size
    with open(p, "rb") as f:
        raw = f.read(max_bytes)
    if b"\x00" in raw[:2048]:
        raise ToolError(f"{p.name} looks like a binary file ({size} bytes)")
    text = raw.decode("utf-8", errors="replace")
    text = redact(text)
    if size > max_bytes:
        text += f"\n... [truncated, {size - max_bytes} more bytes]"
    return text


def list_dir(path: str, cwd: str, limit: int = 200) -> str:
    p = _expand(path or ".", cwd)
    if not p.is_dir():
        raise ToolError(f"not a directory: {p}")
    entries = []
    try:
        for e in sorted(p.iterdir(), key=lambda x: (not x.is_dir(), x.name.lower())):
            try:
                st = e.stat()
                kind = "d" if e.is_dir() else "-"
                size = "" if e.is_dir() else f"{st.st_size}"
                entries.append(f"{kind} {size:>10} {e.name}{'/' if e.is_dir() else ''}")
            except OSError:
                entries.append(f"? {'':>10} {e.name}")
    except PermissionError:
        raise ToolError(f"permission denied: {p}")
    more = f"\n... and {len(entries) - limit} more" if len(entries) > limit else ""
    return f"{p}\n" + "\n".join(entries[:limit]) + more


def find_files(pattern: str, cwd: str, root: str = ".", max_results: int = 100) -> str:
    base = _expand(root, cwd)
    if not base.is_dir():
        raise ToolError(f"not a directory: {base}")
    skip = {".git", "node_modules", "__pycache__", ".cache", ".venv", "venv"}
    hits = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for name in filenames + dirnames:
            if fnmatch.fnmatch(name.lower(), pattern.lower()):
                hits.append(os.path.join(dirpath, name))
                if len(hits) >= max_results:
                    return "\n".join(hits) + f"\n... stopped at {max_results} results"
    return "\n".join(hits) or "no matches"


def grep_files(text: str, cwd: str, root: str = ".", glob: str = "*", max_results: int = 60) -> str:
    base = _expand(root, cwd)
    skip = {".git", "node_modules", "__pycache__", ".cache", ".venv", "venv"}
    try:
        rx = re.compile(text, re.I)
    except re.error:
        rx = re.compile(re.escape(text), re.I)
    hits = []
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = [d for d in dirnames if d not in skip]
        for fn in filenames:
            if not fnmatch.fnmatch(fn, glob):
                continue
            full = os.path.join(dirpath, fn)
            if is_sensitive(full):
                continue
            try:
                if os.path.getsize(full) > 1_000_000:
                    continue
                with open(full, "r", encoding="utf-8", errors="strict") as f:
                    for n, line in enumerate(f, 1):
                        if rx.search(line):
                            hits.append(f"{full}:{n}: {redact(line.strip())[:160]}")
                            if len(hits) >= max_results:
                                return "\n".join(hits) + f"\n... stopped at {max_results} results"
            except (OSError, UnicodeDecodeError):
                continue
    return "\n".join(hits) or "no matches"


# ------------------------------------------------------------------ write
@dataclass
class WritePlan:
    path: Path
    content: str
    exists: bool
    verdict: Verdict
    reason: str
    preview: str  # diff or head of new file


def plan_write(path: str, content: str, cwd: str) -> WritePlan:
    p = _expand(path, cwd)
    if is_sensitive(p):
        raise ToolError(f"refusing to write {p.name}: credential/secret file")
    if not _within(p, _writable_roots()):
        raise ToolError(f"refusing to write outside your home/shared storage: {p}")
    if p.is_dir():
        raise ToolError(f"{p} is a directory")
    exists = p.exists()
    verdict, reason = Verdict.WRITE, "overwrites existing file" if exists else "creates new file"
    if p.name in RC_FILES or ".termux" in p.parts or ".ssh" in p.parts:
        verdict, reason = Verdict.DANGEROUS, "shell startup / Termux / SSH configuration"
    if exists:
        try:
            old = p.read_text(encoding="utf-8").splitlines(keepends=True)
        except (OSError, UnicodeDecodeError):
            old = []
        diff = "".join(difflib.unified_diff(old, content.splitlines(keepends=True), f"a/{p.name}", f"b/{p.name}", n=2))
        preview = diff[:2500] or "(no changes)"
    else:
        lines = content.splitlines()
        preview = "\n".join(lines[:25]) + (f"\n... ({len(lines) - 25} more lines)" if len(lines) > 25 else "")
    return WritePlan(p, content, exists, verdict, reason, preview)


def apply_write(plan: WritePlan) -> str:
    if plan.exists:
        backup_dir = DATA_DIR / "backups"
        backup_dir.mkdir(parents=True, exist_ok=True)
        dest = backup_dir / f"{time.strftime('%Y%m%d-%H%M%S')}_{plan.path.name}"
        try:
            shutil.copy2(plan.path, dest)
        except OSError:
            dest = None
    else:
        dest = None
    plan.path.parent.mkdir(parents=True, exist_ok=True)
    tmp = plan.path.with_name(plan.path.name + ".tmp-termuxai")
    tmp.write_text(plan.content, encoding="utf-8")
    os.replace(tmp, plan.path)
    msg = f"wrote {plan.path} ({len(plan.content)} chars)"
    if dest:
        msg += f"; previous version backed up to {dest}"
    return msg
