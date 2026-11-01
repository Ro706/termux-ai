"""Command safety policy and secret redaction.

Every command proposed by the LLM passes through classify() before the user
even sees an approval prompt. Verdicts:

  READ_ONLY - safe inspection commands; still shown, may be auto-approved if
              the user enables that setting.
  WRITE     - modifies state; always needs explicit approval.
  DANGEROUS - destructive/privileged/sensitive; needs approval plus an extra
              confirmation, and is flagged in red.
  BLOCKED   - never executed under any circumstances.
"""
from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from enum import Enum


class Verdict(str, Enum):
    READ_ONLY = "read-only"
    WRITE = "write"
    DANGEROUS = "dangerous"
    BLOCKED = "blocked"


@dataclass
class Classification:
    verdict: Verdict
    reason: str


READ_ONLY_CMDS = {
    "ls", "pwd", "cat", "head", "tail", "wc", "echo", "date", "whoami", "uname",
    "df", "du", "free", "uptime", "ps", "top", "env", "printenv", "which", "type",
    "file", "stat", "find", "grep", "rg", "tree", "id", "hostname", "ping",
    "nslookup", "dig", "ip", "ifconfig", "netstat", "ss", "termux-info",
    "termux-battery-status", "termux-wifi-connectioninfo", "python", "python3",
    "pip", "node", "git", "pkg", "apt", "sort", "uniq", "diff", "less", "more",
    "basename", "dirname", "realpath", "readlink", "lscpu", "getprop",
}

# Subcommand rules for multi-purpose tools: tool -> set of read-only subcommands
READ_ONLY_SUBCMDS = {
    "git": {"status", "log", "diff", "show", "branch", "remote", "config", "blame", "ls-files", "tag", "rev-parse"},
    "pkg": {"list-installed", "search", "show", "list-all", "files"},
    "apt": {"list", "search", "show", "policy"},
    "pip": {"list", "show", "freeze", "check", "--version"},
}

# Interpreters are only read-only for version queries.
INTERPRETERS = {"python", "python3", "node"}

WRITE_CMDS = {
    "mkdir", "touch", "cp", "mv", "ln", "chmod", "tee", "sed", "pip", "pkg", "apt",
    "git", "npm", "yarn", "cargo", "make", "gcc", "g++", "clang", "javac", "java",
    "python", "python3", "node", "bash", "sh", "curl", "wget", "tar", "unzip", "zip",
    "gzip", "gunzip", "nano", "vim", "ssh-keygen", "termux-setup-storage",
}

DANGEROUS_CMDS = {"rm", "rmdir", "kill", "killall", "pkill", "chown", "shred", "truncate", "ssh", "scp", "sftp", "rsync"}

# Never run, regardless of approval.
BLOCKED_PATTERNS = [
    (r"\bsu\b(\s|$)", "privilege escalation (su)"),
    (r"\bsudo\b", "privilege escalation (sudo)"),
    (r"\btsu\b", "privilege escalation (tsu)"),
    (r"\bdoas\b", "privilege escalation (doas)"),
    (r"\brm\s+(-[a-zA-Z]*\s+)*-[a-zA-Z]*[rR][a-zA-Z]*\s+(-[a-zA-Z]+\s+)*(/|~|\$HOME|\$PREFIX|/data|/sdcard|/storage|\*)(\s|/?$)", "unrestricted recursive deletion"),
    (r"\brm\s+.*--no-preserve-root", "rm --no-preserve-root"),
    (r"\bmkfs(\.\w+)?\b", "disk formatting"),
    (r"\b(fdisk|sfdisk|parted|gdisk|wipefs)\b", "partition tool"),
    (r"\bdd\b.*\bof=/dev/", "raw write to a device"),
    (r">\s*/dev/(sd|mmcblk|block|nvme)", "raw write to a block device"),
    (r":\(\)\s*\{\s*:\|:&\s*\};:", "fork bomb"),
    (r"\bchmod\s+(-R\s+)?[0-7]*777\s+(/|~|\$HOME|\$PREFIX)(\s|$)", "recursive world-writable permissions on a root path"),
    (r"(curl|wget)[^|;&]*\|\s*(sudo\s+)?(ba|z|da)?sh\b", "piping a download straight into a shell"),
    (r"\b(mount|umount)\b", "mount operations"),
    (r"\b(reboot|shutdown|poweroff|halt)\b", "power control"),
    (r"\bsetprop\b", "system property modification"),
    (r"\bpm\s+(uninstall|clear|disable)", "Android package manager destructive action"),
]

# Credential / secret access.
SENSITIVE_PATH_PATTERNS = [
    r"\.ssh/(id_[a-z0-9]+|.*_key)(?!\.pub)\b",
    r"\.gnupg\b",
    r"\.netrc\b",
    r"\.aws/credentials",
    r"\.git-credentials",
    r"\.config/gh/hosts\.yml",
    r"\.env\b",
    r"/etc/shadow",
    r"\.bash_history|\.zsh_history",
    r"\.mozilla|\.config/google-chrome",
]

SHELL_META = re.compile(r"[;&|`]|\$\(|>>?|<\(")


def blocked_reason(command: str) -> str | None:
    for pat, why in BLOCKED_PATTERNS:
        if re.search(pat, command):
            return why
    for pat in SENSITIVE_PATH_PATTERNS:
        if re.search(pat, command) and re.search(r"\b(cat|less|more|head|tail|cp|mv|scp|curl|wget|base64|xxd|strings|grep|rg|tar|zip)\b", command):
            return "reads or transmits credentials/secrets"
    return None


def _split_segments(command: str) -> list[str]:
    return [s.strip() for s in re.split(r"&&|\|\||;|\|", command) if s.strip()]


def _segment_verdict(seg: str) -> Verdict:
    try:
        parts = shlex.split(seg)
    except ValueError:
        return Verdict.WRITE
    # strip env assignments and leading "command"/"time"
    while parts and ("=" in parts[0] and not parts[0].startswith("-") or parts[0] in ("command", "time", "nohup")):
        parts.pop(0)
    if not parts:
        return Verdict.READ_ONLY
    cmd = os.path.basename(parts[0])
    args = parts[1:]

    if cmd in DANGEROUS_CMDS:
        return Verdict.DANGEROUS
    if cmd == "find" and any(a in ("-delete", "-exec", "-execdir", "-ok") for a in args):
        return Verdict.DANGEROUS if "-delete" in args else Verdict.WRITE
    if cmd == "sed" and "-i" not in " ".join(args) and not any(a.startswith("-i") for a in args):
        return Verdict.READ_ONLY
    if cmd in INTERPRETERS:
        if args and args[0] in ("--version", "-V", "-v"):
            return Verdict.READ_ONLY
        return Verdict.WRITE
    if cmd in READ_ONLY_SUBCMDS:
        sub = next((a for a in args if not a.startswith("-")), args[0] if args else "")
        if sub in READ_ONLY_SUBCMDS[cmd] or (args and args[0] == "--version"):
            return Verdict.READ_ONLY
        if cmd in ("pkg", "apt") and sub in ("remove", "purge", "autoremove", "uninstall"):
            return Verdict.DANGEROUS
        if cmd == "git" and sub in ("reset", "clean", "push") and any(a in ("--hard", "-f", "--force", "-fd", "-fdx", "-x") for a in args):
            return Verdict.DANGEROUS
        return Verdict.WRITE
    if cmd in READ_ONLY_CMDS and cmd not in WRITE_CMDS:
        return Verdict.READ_ONLY
    if cmd in WRITE_CMDS:
        return Verdict.WRITE
    return Verdict.WRITE  # unknown commands default to needing approval


def classify(command: str) -> Classification:
    command = command.strip()
    if not command:
        return Classification(Verdict.BLOCKED, "empty command")
    why = blocked_reason(command)
    if why:
        return Classification(Verdict.BLOCKED, why)

    rank = {Verdict.READ_ONLY: 0, Verdict.WRITE: 1, Verdict.DANGEROUS: 2}
    worst = Verdict.READ_ONLY
    reason = "inspection only"
    for seg in _split_segments(command):
        v = _segment_verdict(seg)
        if rank[v] > rank[worst]:
            worst, reason = v, f"'{seg.split()[0]}' {'is destructive or sensitive' if v is Verdict.DANGEROUS else 'may modify state'}"
    # output redirection or command substitution means it is not purely read-only
    if worst is Verdict.READ_ONLY and re.search(r">|\$\(|`", command):
        worst, reason = Verdict.WRITE, "uses redirection or command substitution"
    return Classification(worst, reason)


# ---------------------------------------------------------------- redaction

_SECRET_KEY_RE = re.compile(r"(key|token|secret|passw(or)?d|passwd|credential|auth|cookie|session|private)", re.I)
_TOKEN_PATTERNS = [
    re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}"),
    re.compile(r"\bgh[pousr]_[A-Za-z0-9]{20,}"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9\-]{10,}"),
    re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}"),
    re.compile(r"eyJ[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}\.[A-Za-z0-9_\-]{10,}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
]
_ASSIGN_RE = re.compile(r"(?im)^(\s*(?:export\s+)?[A-Za-z_][A-Za-z0-9_]*)\s*[=:]\s*(.+)$")


def redact(text: str) -> str:
    """Remove obvious secrets before text is sent to the LLM or stored."""
    for pat in _TOKEN_PATTERNS:
        text = pat.sub("[REDACTED]", text)

    def _assign(m: re.Match) -> str:
        return f"{m.group(1)}=[REDACTED]" if _SECRET_KEY_RE.search(m.group(1)) else m.group(0)

    return _ASSIGN_RE.sub(_assign, text)


def safe_environ() -> dict[str, str]:
    """Environment for the LLM: sensitive-looking variables are dropped."""
    return {k: v for k, v in os.environ.items() if not _SECRET_KEY_RE.search(k)}
