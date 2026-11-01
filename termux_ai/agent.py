"""The agent: prompt building, streaming, tool-call protocol, approvals, learning.

Local models rarely have reliable native tool calling, so the protocol is
plain text: the model emits fenced ```action blocks containing one JSON object.
Nothing in those blocks runs until it passes the security policy and, for
anything that isn't read-only, explicit user approval.
"""
from __future__ import annotations

import asyncio
import json
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Awaitable, Callable

from . import sysinfo, tools
from .config import Config
from .executor import CommandRejected, Executor
from .memory import MemoryStore
from .news import fetch_category
from .ollama_client import OllamaClient, OllamaError
from .search import web_search
from .security import Verdict, classify, redact

MAX_STEPS = 6
ACTION_RE = re.compile(r"```action\s*\n(.*?)```", re.S)
FENCE_OPEN = "```action"

SYSTEM_PROMPT = """You are Termux AI, a personal terminal assistant running inside Termux on an Android phone.
You help with shell work, files, code, packages, git, and system health. Be concise and practical.

ENVIRONMENT (not a desktop Linux): Android/Termux, packages via `pkg`, no systemd, no root, no sudo.
Use `pkg install` for system packages and `pip` for Python. Prefer Termux-friendly solutions.

TOOLS. To act, emit a fenced block with exactly one JSON object, like:
```action
{{"tool": "run", "command": "ls -la"}}
```
Available tools:
- run: {{"tool":"run","command":"<shell command>","why":"<short reason>"}}
- read_file: {{"tool":"read_file","path":"<path>"}}
- list_dir: {{"tool":"list_dir","path":"<path>"}}
- find: {{"tool":"find","pattern":"*.py","root":"."}}
- grep: {{"tool":"grep","text":"<regex>","root":".","glob":"*.py"}}
- write_file: {{"tool":"write_file","path":"<path>","content":"<full file content>"}}
- system: {{"tool":"system"}}  (live CPU/RAM/storage/battery snapshot)
- cache_scan: {{"tool":"cache_scan"}}  (list cache dirs and sizes; read-only)
- web_search: {{"tool":"web_search","query":"<query>"}}
- news: {{"tool":"news","category":"tech|ai|general"}}
- remember: {{"tool":"remember","kind":"preference|project|command|environment|solution|decision|fact","title":"<short>","body":"<detail>"}}
- task: {{"tool":"task","title":"<title>","status":"pending|done|failed","next_steps":"<optional>"}}

RULES:
- Emit at most 3 action blocks per reply, then STOP and wait; results come back to you.
- The user sees and must approve every command and file change. Never claim something ran before you see its result.
- Explain briefly what you are about to do before an action block. After results, explain them plainly.
- Never use sudo/su/root. Never run rm -rf on broad paths. Never read credentials, keys, tokens, or .env files.
- Before modifying a file to fix an error, read it first and show the intended change.
- For cleanup, list candidates and sizes first (cache_scan), explain, then propose specific deletions.
- Use `remember` only for durable, reusable facts (preferences, working fixes, project setup), not chit-chat.
- If no action is needed, just answer in plain text.

CURRENT CONTEXT
Time: {now}
Working directory: {cwd}
Project: {project}
{env}

{memory}
"""


@dataclass
class ApprovalRequest:
    kind: str  # "command" | "write_file"
    title: str
    detail: str
    verdict: Verdict
    reason: str


async def _noop(*_a, **_k):
    return None


@dataclass
class Hooks:
    on_status: Callable[[str], Awaitable[None]] = _noop
    on_token: Callable[[str], Awaitable[None]] = _noop
    on_tool: Callable[[str, str, bool], Awaitable[None]] = _noop  # title, output, ok
    approve: Callable[[ApprovalRequest], Awaitable[bool]] = None  # type: ignore[assignment]


def visible_text(full: str) -> str:
    """Hide ```action blocks (complete, in-progress, or about to start) from display."""
    out = ACTION_RE.sub("", full)
    idx = out.find(FENCE_OPEN)
    if idx != -1:
        out = out[:idx]
    else:
        for n in range(len(FENCE_OPEN) - 1, 0, -1):
            if out.endswith(FENCE_OPEN[:n]):
                out = out[:-n]
                break
    return out


def parse_actions(text: str) -> list[dict]:
    actions = []
    for m in ACTION_RE.finditer(text):
        raw = m.group(1).strip()
        try:
            obj = json.loads(raw)
        except json.JSONDecodeError:
            # tolerate trailing commas / single stray text
            try:
                obj = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
            except (ValueError, json.JSONDecodeError):
                actions.append({"tool": "_invalid", "raw": raw[:200]})
                continue
        if isinstance(obj, dict) and "tool" in obj:
            actions.append(obj)
    return actions[:3]


class Agent:
    def __init__(self, cfg: Config, mem: MemoryStore, ollama: OllamaClient, executor: Executor):
        self.cfg = cfg
        self.mem = mem
        self.ollama = ollama
        self.exe = executor
        self.session = uuid.uuid4().hex[:12]
        self.model = cfg.model
        self.project = ""
        self.status = "Idle"
        self.last_interaction: float | None = None
        self.static = sysinfo.static_info()
        self.auto_learn = True
        self.cancelled = False

    # -------------------------------------------------------- lifecycle
    async def detect_model(self) -> str:
        """Pick the configured model if installed, else the first installed one."""
        try:
            models = await self.ollama.list_models()
        except OllamaError:
            return self.model
        names = [m.name for m in models]
        if self.model and (self.model in names or f"{self.model}:latest" in names):
            return self.model
        if names:
            self.model = names[0]
        return self.model

    def restore(self) -> str:
        """Restore the last working context after a restart."""
        last = self.mem.last_session()
        if last:
            self.session = last
        cwd = self.mem.get_context("cwd")
        if cwd:
            try:
                self.exe.chdir(cwd)
            except FileNotFoundError:
                pass
        self.project = self.mem.get_context("project")
        model = self.mem.get_context("model")
        if model and not self.cfg.model:
            self.model = model
        return self.mem.resume_summary()

    def set_project(self, name: str) -> None:
        self.project = name
        self.mem.set_context("project", name)

    def persist_context(self) -> None:
        self.mem.set_context("cwd", self.exe.cwd)
        self.mem.set_context("project", self.project)
        if self.model:
            self.mem.set_context("model", self.model)

    def cancel(self) -> None:
        self.cancelled = True
        self.exe.cancel()

    # ----------------------------------------------------------- prompt
    def _system_prompt(self, user_text: str) -> str:
        mems = self.mem.relevant_for(user_text, 5)
        prefs = self.mem.preferences(5)
        seen, lines = set(), []
        for m in list(prefs) + mems:
            if m.id in seen:
                continue
            seen.add(m.id)
            lines.append(f"- [{m.kind}] {m.title}: {m.body}")
        open_tasks = self.mem.open_tasks()[:4]
        memory = ""
        if lines:
            memory += "REMEMBERED (from earlier sessions):\n" + "\n".join(lines) + "\n"
        if open_tasks:
            memory += "OPEN TASKS:\n" + "\n".join(f"- #{t.id} [{t.status}] {t.title}" + (f" (next: {t.next_steps})" if t.next_steps else "") for t in open_tasks) + "\n"
        recent = self.mem.last_commands(4)
        if recent:
            memory += "RECENT COMMANDS: " + "; ".join(redact(r["command"])[:60] for r in recent) + "\n"
        env = "\n".join(f"{k}={v}" for k, v in sysinfo.env_for_llm().items() if k in ("HOME", "PREFIX", "SHELL"))
        env += f"\nDevice: {self.static.get('device')}, Android {self.static.get('android')}, {self.static.get('arch')}"
        return SYSTEM_PROMPT.format(
            now=time.strftime("%Y-%m-%d %H:%M"),
            cwd=self.exe.cwd,
            project=self.project or "(none)",
            env=env,
            memory=memory or "(no relevant memories yet)",
        )

    # ------------------------------------------------------------- turn
    async def respond(self, user_text: str, hooks: Hooks) -> None:
        if not self.model:
            await self.detect_model()
        if not self.model:
            await hooks.on_token("No Ollama model found. Run `ollama pull <model>` (e.g. a small one like qwen2.5:3b) and try again.")
            return
        self.cancelled = False
        self.last_interaction = time.time()
        self.mem.add_message(self.session, "user", redact(user_text))
        ran_actions, failures = [], []

        for step in range(MAX_STEPS):
            if self.cancelled:
                break
            await hooks.on_status("Thinking")
            history = self.mem.recent_messages(self.session, self.cfg.history_turns * 2)
            messages = [{"role": "system", "content": self._system_prompt(user_text)}] + history
            full, emitted = "", ""
            try:
                async for piece in self.ollama.chat_stream(self.model, messages, self.cfg.temperature, self.cfg.num_ctx):
                    if self.cancelled:
                        break
                    full += piece
                    vis = visible_text(full)
                    if vis.startswith(emitted) and len(vis) > len(emitted):
                        await hooks.on_token(vis[len(emitted):])
                        emitted = vis
            except OllamaError as e:
                await hooks.on_token(f"\n[Ollama error] {e}")
                await hooks.on_status("Idle")
                return
            self.mem.add_message(self.session, "assistant", full)
            if self.cancelled:
                await hooks.on_token("\n[cancelled]")
                break

            actions = parse_actions(full)
            if not actions:
                break

            results = []
            for act in actions:
                if self.cancelled:
                    break
                await hooks.on_token("\n")
                title, output, ok = await self._execute(act, hooks)
                ran_actions.append(act)
                if not ok:
                    failures.append((act, output))
                results.append(f"[{title}] {'OK' if ok else 'FAILED'}\n{output}")
                await hooks.on_tool(title, output, ok)
            feedback = "TOOL RESULTS:\n" + "\n\n".join(results) + "\n\nContinue: explain results, then act again only if needed."
            self.mem.add_message(self.session, "user", feedback)
            await hooks.on_token("\n")
        else:
            await hooks.on_token("\n[stopped after maximum steps; ask me to continue]")

        await hooks.on_status("Idle")
        self.persist_context()
        if self.auto_learn and ran_actions and not self.cancelled:
            await self._learn(user_text, ran_actions, failures)

    # ------------------------------------------------------- tool dispatch
    async def _execute(self, act: dict, hooks: Hooks) -> tuple[str, str, bool]:
        tool = act.get("tool")
        cwd = self.exe.cwd
        try:
            if tool == "run":
                return await self._tool_run(str(act.get("command", "")), str(act.get("why", "")), hooks)
            if tool == "read_file":
                return f"read {act.get('path')}", tools.read_file(str(act.get("path", "")), cwd), True
            if tool == "list_dir":
                return f"ls {act.get('path', '.')}", tools.list_dir(str(act.get("path", ".")), cwd), True
            if tool == "find":
                return f"find {act.get('pattern')}", tools.find_files(str(act.get("pattern", "*")), cwd, str(act.get("root", "."))), True
            if tool == "grep":
                return f"grep {act.get('text')}", tools.grep_files(str(act.get("text", "")), cwd, str(act.get("root", ".")), str(act.get("glob", "*"))), True
            if tool == "write_file":
                return await self._tool_write(str(act.get("path", "")), str(act.get("content", "")), hooks)
            if tool == "system":
                snap = await sysinfo.snapshot_async(self.static)
                return "system snapshot", snap.summary(), True
            if tool == "cache_scan":
                rows = await asyncio.to_thread(sysinfo.cache_candidates)
                body = "\n".join(f"{_human(sz):>10}  {p}" for p, sz in rows) or "no cache directories found"
                return "cache scan", body, True
            if tool == "web_search":
                res = await web_search(str(act.get("query", "")))
                body = "\n".join(f"- {r.title}\n  {r.url}\n  {r.snippet}" for r in res) or "no results"
                return f"search: {act.get('query')}", body, True
            if tool == "news":
                cat = str(act.get("category", "tech"))
                items = await fetch_category(self.cfg.feeds.get(cat, self.cfg.feeds["tech"]), 8)
                body = "\n".join(f"- {i.title} ({i.source})\n  {i.link}" for i in items) or "no items (offline?)"
                return f"news: {cat}", body, True
            if tool == "remember":
                mid = self.mem.add_memory(str(act.get("kind", "fact")), redact(str(act.get("title", ""))), redact(str(act.get("body", ""))), self.project or None)
                return "remember", f"saved memory #{mid}", True
            if tool == "task":
                tid = self.mem.add_task(str(act.get("title", "")), str(act.get("status", "pending")), self.project or None, None, act.get("next_steps"))
                return "task", f"recorded task #{tid}", True
            if tool == "_invalid":
                return "invalid action", f"could not parse action JSON: {act.get('raw')}", False
            return f"unknown tool {tool}", "unsupported tool; use one from the list", False
        except (tools.ToolError, CommandRejected) as e:
            return f"{tool}", str(e), False
        except Exception as e:  # keep the agent alive on any tool bug
            return f"{tool}", f"{type(e).__name__}: {e}", False

    async def run_command(self, command: str, hooks: Hooks) -> tuple[str, str, bool]:
        """User-typed command (the `!cmd` path): same policy and approval gate as the LLM's."""
        self.last_interaction = time.time()
        try:
            return await self._tool_run(command, "", hooks)
        finally:
            await hooks.on_status("Idle")
            self.persist_context()

    async def _tool_run(self, command: str, why: str, hooks: Hooks) -> tuple[str, str, bool]:
        cls = classify(command)
        if cls.verdict is Verdict.BLOCKED:
            self.exe._log(command, "blocked", cls.reason)
            return f"$ {command}", f"BLOCKED by security policy: {cls.reason}. Do not retry this; propose a safer alternative.", False
        if cls.verdict is not Verdict.READ_ONLY:
            approved = await self._ask(hooks, ApprovalRequest("command", f"Run command ({cls.verdict.value})", f"$ {command}\n\nin: {self.exe.cwd}\n{('why: ' + why) if why else ''}".strip(), cls.verdict, cls.reason))
            if not approved:
                return f"$ {command}", "User declined. Do not run it; ask what they'd prefer.", False
        else:
            await hooks.on_tool(f"$ {command}", "(read-only, running)", True)
        await hooks.on_status("Executing")
        res = await self.exe.run(command, approved=True)
        self.mem.log_command(redact(command), self.exe.cwd, cls.verdict.value, res.exit_code)
        flag = " (timed out)" if res.timed_out else ""
        return f"$ {command}", f"exit {res.exit_code}{flag}\n{res.output}".strip(), res.ok

    async def _tool_write(self, path: str, content: str, hooks: Hooks) -> tuple[str, str, bool]:
        plan = tools.plan_write(path, content, self.exe.cwd)
        approved = await self._ask(hooks, ApprovalRequest("write_file", f"Write file: {plan.path}", plan.preview, plan.verdict, plan.reason))
        if not approved:
            return f"write {plan.path}", "User declined the file change.", False
        return f"write {plan.path}", tools.apply_write(plan), True

    async def _ask(self, hooks: Hooks, req: ApprovalRequest) -> bool:
        if hooks.approve is None:
            return False  # fail closed
        await hooks.on_status("Awaiting approval")
        try:
            return bool(await hooks.approve(req))
        finally:
            await hooks.on_status("Thinking")

    # ----------------------------------------------------------- learning
    async def _learn(self, user_text: str, actions: list[dict], failures: list) -> None:
        """Extract durable knowledge from this turn. Best-effort; never raises."""
        if any(a.get("tool") == "remember" for a in actions) and not failures:
            return  # the model already curated memory itself
        summary = []
        for a in actions:
            if a.get("tool") in ("remember", "task", "_invalid"):
                continue
            tag = a.get("command") or a.get("path") or a.get("query") or a.get("tool")
            summary.append(f"{a.get('tool')}: {str(tag)[:100]}")
        if not summary:
            return
        failed = "; ".join(f"{a.get('tool')} -> {out[:120]}" for a, out in failures[:3])
        prompt = (
            "Decide what from this terminal session is worth remembering long-term. Reply with ONLY JSON:\n"
            '{"task":{"title":"","status":"done|failed|pending","next_steps":""},'
            '"memories":[{"kind":"preference|project|command|environment|solution|decision|fact","title":"","body":""}]}\n'
            "Only include memories that are durable and reusable (a fix that worked, a preference, project setup). "
            "Use an empty memories list if nothing qualifies. Never include secrets.\n\n"
            f"User request: {redact(user_text)[:300]}\nActions: {' | '.join(summary)[:600]}\nFailures: {failed or 'none'}"
        )
        try:
            raw = await self.ollama.chat(self.model, [{"role": "user", "content": prompt}], temperature=0.1, num_ctx=self.cfg.num_ctx)
            data = json.loads(raw[raw.index("{"): raw.rindex("}") + 1])
        except Exception:
            # fall back to a plain task record so /lasttask and /resume still work
            self.mem.add_task(user_text[:120], "failed" if failures else "done", self.project or None)
            return
        t = data.get("task") or {}
        if t.get("title"):
            status = t.get("status") if t.get("status") in ("done", "failed", "pending") else "done"
            self.mem.add_task(str(t["title"]), status, self.project or None, None, t.get("next_steps") or None)
        for m in (data.get("memories") or [])[:3]:
            if isinstance(m, dict) and m.get("title") and m.get("body"):
                self.mem.add_memory(str(m.get("kind", "fact")), redact(str(m["title"])), redact(str(m["body"])), self.project or None)


def _human(n: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024 or unit == "GB":
            return f"{n:.1f} {unit}" if unit != "B" else f"{int(n)} B"
        n /= 1024
    return "?"
