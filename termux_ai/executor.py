"""Safe command execution: classification gate, timeout, cancellation, logging.

Commands are run with shell=False via `bash -c`-free argv where possible. Shell
operators (pipes, &&, redirects) need a shell, so those go through `sh -c`, but
only after the full string has been classified and approved.
"""
from __future__ import annotations

import asyncio
import os
import shlex
import shutil
import signal
import time
from dataclasses import dataclass
from pathlib import Path

from .security import Classification, Verdict, classify, redact, safe_environ

_NEEDS_SHELL = ("|", "&&", "||", ";", ">", "<", "$(", "`", "*", "?", "~")


@dataclass
class ExecResult:
    command: str
    exit_code: int | None
    output: str
    duration: float
    timed_out: bool = False
    cancelled: bool = False

    @property
    def ok(self) -> bool:
        return self.exit_code == 0


class CommandRejected(Exception):
    pass


class Executor:
    def __init__(self, log_path: Path, timeout: int = 60, max_chars: int = 6000):
        self.log_path = Path(log_path)
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.timeout = timeout
        self.max_chars = max_chars
        self.cwd = os.getcwd()
        self._proc: asyncio.subprocess.Process | None = None

    # -------------------------------------------------------------- state
    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    def chdir(self, path: str) -> str:
        target = Path(os.path.expanduser(path))
        if not target.is_absolute():
            target = Path(self.cwd) / target
        target = target.resolve()
        if not target.is_dir():
            raise FileNotFoundError(f"not a directory: {target}")
        self.cwd = str(target)
        return self.cwd

    def check(self, command: str) -> Classification:
        return classify(command)

    # ---------------------------------------------------------------- run
    async def run(self, command: str, approved: bool, timeout: int | None = None) -> ExecResult:
        """Run a command. `approved` must be True for anything but READ_ONLY."""
        cls = classify(command)
        if cls.verdict is Verdict.BLOCKED:
            self._log(command, cls.verdict.value, "BLOCKED: " + cls.reason)
            raise CommandRejected(f"Blocked: {cls.reason}")
        if cls.verdict is not Verdict.READ_ONLY and not approved:
            raise CommandRejected(f"'{cls.verdict.value}' command needs explicit approval")

        timeout = timeout or self.timeout
        # cd is handled in-process so the working directory persists
        stripped = command.strip()
        if stripped == "cd" or stripped.startswith("cd "):
            try:
                new = self.chdir(stripped[2:].strip() or "~")
                self._log(command, cls.verdict.value, f"cwd -> {new}")
                return ExecResult(command, 0, f"cwd: {new}", 0.0)
            except FileNotFoundError as e:
                return ExecResult(command, 1, str(e), 0.0)

        start = time.monotonic()
        env = dict(os.environ)
        env["TERM"] = "dumb"
        env["GIT_TERMINAL_PROMPT"] = "0"
        try:
            if any(tok in command for tok in _NEEDS_SHELL):
                sh = shutil.which("bash") or shutil.which("sh") or "/bin/sh"
                self._proc = await asyncio.create_subprocess_exec(
                    sh, "-c", command, cwd=self.cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                )
            else:
                argv = shlex.split(command)
                self._proc = await asyncio.create_subprocess_exec(
                    *argv, cwd=self.cwd, env=env, stdin=asyncio.subprocess.DEVNULL,
                    stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, start_new_session=True,
                )
        except (FileNotFoundError, PermissionError, ValueError) as e:
            self._log(command, cls.verdict.value, f"spawn failed: {e}")
            return ExecResult(command, 127, f"cannot start command: {e}", 0.0)

        timed_out = cancelled = False
        try:
            out, _ = await asyncio.wait_for(self._proc.communicate(), timeout=timeout)
        except asyncio.TimeoutError:
            timed_out = True
            out = await self._kill_and_collect()
        except asyncio.CancelledError:
            cancelled = True
            out = await self._kill_and_collect()
        finally:
            code = self._proc.returncode if self._proc else None
            self._proc = None

        text = out.decode(errors="replace") if out else ""
        text = redact(text)
        if len(text) > self.max_chars:
            half = self.max_chars // 2
            text = text[:half] + f"\n... [{len(text) - self.max_chars} chars truncated] ...\n" + text[-half:]
        if timed_out:
            text += f"\n[timed out after {timeout}s]"
        res = ExecResult(command, code, text, time.monotonic() - start, timed_out, cancelled)
        self._log(command, cls.verdict.value, f"exit={code} {res.duration:.1f}s" + (" TIMEOUT" if timed_out else "") + (" CANCELLED" if cancelled else ""))
        return res

    async def _kill_and_collect(self) -> bytes:
        proc = self._proc
        if not proc:
            return b""
        try:
            os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
        except (ProcessLookupError, PermissionError):
            pass
        try:
            out, _ = await asyncio.wait_for(proc.communicate(), timeout=3)
        except asyncio.TimeoutError:
            try:
                os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
            out, _ = await proc.communicate()
        return out or b""

    def cancel(self) -> bool:
        """Terminate the running command (SIGTERM to its process group)."""
        if self.running:
            try:
                os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
                return True
            except (ProcessLookupError, PermissionError):
                return False
        return False

    # ---------------------------------------------------------------- log
    def _log(self, command: str, verdict: str, note: str) -> None:
        line = f"{time.strftime('%Y-%m-%d %H:%M:%S')}\t{verdict}\t{self.cwd}\t{redact(command)}\t{note}\n"
        try:
            with open(self.log_path, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError:
            pass
