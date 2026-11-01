"""Slash commands. Each handler returns plain text for the chat log (or None)."""
from __future__ import annotations

import shlex
import time
from pathlib import Path

from . import sysinfo
from .agent import Agent
from .config import save_config
from .memory import MEMORY_KINDS
from .news import fetch_category
from .search import web_search

HELP = """Commands
  /memory                  list memories (/memory <kind> to filter)
  /memory search <text>    search memories
  /memory add <kind> <title> | <body>
  /memory edit <id> <new body>
  /memory delete <id>
  /memory clear [memories|history|tasks|commands|all]   (asks to confirm with --yes)
  /memory export [path]    export memories + tasks as JSON
  /history                 recent conversation
  /lasttask                show the last task and next steps
  /tasks                   list open tasks
  /resume                  restore last working context
  /model [name]            list models or switch
  /cd <path>   /cwd        change / show working directory
  /project <name>          set the current project
  /sys                     system snapshot
  /news [tech|ai|general]  headlines
  /search <query>          web search
  /learn on|off            toggle automatic memory extraction
  /open <screen>           files | monitor | news | projects | memory | settings | dash
  !<command>               run a command directly (same safety checks and approvals)
  /cancel  (or Esc)        stop the running command / generation
  /clear                   clear the screen   /quit  exit
  Keys: F1 dashboard, F2 files, F3 monitor, F4 news, F5 projects, F6 memory, F7 settings
Anything else is sent to the AI."""


def _fmt_ts(ts: float) -> str:
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))


async def handle(agent: Agent, line: str) -> str | None:
    """Return text to display, or None if the line isn't a slash command."""
    if not line.startswith("/"):
        return None
    try:
        parts = shlex.split(line)
    except ValueError:
        parts = line.split()
    cmd, args = parts[0].lower(), parts[1:]
    mem = agent.mem

    if cmd in ("/help", "/?"):
        return HELP

    if cmd == "/memory":
        sub = args[0].lower() if args else ""
        if sub == "search":
            q = " ".join(args[1:])
            found = mem.search_memories(q) if q else []
            return "\n".join(f"#{m.id} [{m.kind}] {m.title}\n    {m.body}" for m in found) or "No matching memories."
        if sub == "add":
            rest = line.split(None, 2)[2] if len(args) >= 2 else ""
            if "|" not in rest:
                return "Usage: /memory add <kind> <title> | <body>"
            head, body = (s.strip() for s in rest.split("|", 1))
            kind, _, title = head.partition(" ")
            if kind not in MEMORY_KINDS:
                return f"Kind must be one of: {', '.join(MEMORY_KINDS)}"
            return f"Saved memory #{mem.add_memory(kind, title or body[:40], body, agent.project or None)}"
        if sub == "edit":
            if len(args) < 3 or not args[1].isdigit():
                return "Usage: /memory edit <id> <new body>"
            return "Updated." if mem.update_memory(int(args[1]), body=" ".join(args[2:])) else "No such memory."
        if sub in ("delete", "rm"):
            if len(args) < 2 or not args[1].isdigit():
                return "Usage: /memory delete <id>"
            return "Deleted." if mem.delete_memory(int(args[1])) else "No such memory."
        if sub == "clear":
            target = next((a for a in args[1:] if not a.startswith("--")), "memories")
            if "--yes" not in args:
                return f"This permanently deletes '{target}'. Run `/memory clear {target} --yes` to confirm (use /memory export first to keep a copy)."
            try:
                return f"Cleared {mem.clear(target)} rows from '{target}'."
            except ValueError as e:
                return str(e)
        if sub == "export":
            dest = Path(args[1]).expanduser() if len(args) > 1 else Path.home() / f"termux-ai-memory-{time.strftime('%Y%m%d-%H%M%S')}.json"
            return f"Exported to {mem.export(dest)}"
        kind = sub if sub in MEMORY_KINDS else None
        items = mem.list_memories(kind)
        if not items:
            return "No memories yet. They are created as you work, or with /memory add."
        return "\n".join(f"#{m.id} [{m.kind}] {m.title}  ({_fmt_ts(m.updated)}, used {m.uses}x)\n    {m.body[:160]}" for m in items)

    if cmd == "/history":
        rows = [r for r in mem.history(24) if not r["content"].startswith("TOOL RESULTS:")]
        return "\n".join(f"[{_fmt_ts(r['ts'])}] {r['role']}: {r['content'][:200].replace(chr(10), ' ')}" for r in rows) or "No history."

    if cmd == "/lasttask":
        t = mem.last_task()
        if not t:
            return "No tasks recorded yet."
        return f"#{t.id} [{t.status}] {t.title}\nupdated {_fmt_ts(t.updated)}" + (f"\nNext steps: {t.next_steps}" if t.next_steps else "")

    if cmd == "/tasks":
        ts = mem.open_tasks()
        return "\n".join(f"#{t.id} [{t.status}] {t.title}" + (f" -> {t.next_steps}" if t.next_steps else "") for t in ts) or "No open tasks."

    if cmd == "/resume":
        summary = agent.restore()
        recent = [r for r in mem.history(6) if not r["content"].startswith("TOOL RESULTS:")]
        tail = "\n".join(f"  {r['role']}: {r['content'][:120].replace(chr(10), ' ')}" for r in recent)
        return f"Restored context.\n{summary}" + (f"\n\nRecent:\n{tail}" if tail else "")

    if cmd == "/model":
        if args:
            names = [m.name for m in await agent.ollama.list_models()]
            match = next((n for n in names if n == args[0] or n.split(":")[0] == args[0]), None)
            if not match:
                return f"Not installed: {args[0]}. Installed: {', '.join(names) or 'none'}"
            agent.model = match
            agent.cfg.model = match
            save_config(agent.cfg)
            agent.persist_context()
            return f"Model set to {match}"
        models = await agent.ollama.list_models()
        return "\n".join(f"{'*' if m.name == agent.model else ' '} {m.name}  {m.size_human} {m.parameter_size}" for m in models) or "No models installed (ollama pull <model>)."

    if cmd == "/cwd":
        return agent.exe.cwd
    if cmd == "/cd":
        try:
            new = agent.exe.chdir(" ".join(args) or "~")
            agent.persist_context()
            return f"cwd: {new}"
        except FileNotFoundError as e:
            return str(e)

    if cmd == "/project":
        if not args:
            return f"Project: {agent.project or '(none)'}"
        agent.set_project(" ".join(args))
        return f"Project set to '{agent.project}'"

    if cmd == "/sys":
        return (await sysinfo.snapshot_async(agent.static)).summary()

    if cmd == "/news":
        cat = args[0] if args and args[0] in agent.cfg.feeds else "tech"
        items = await fetch_category(agent.cfg.feeds[cat], 12)
        return f"{cat.upper()} headlines\n" + ("\n".join(f"- {i.title}\n    {i.source}  {i.link}" for i in items) or "No items (offline or feeds unreachable).")

    if cmd == "/search":
        if not args:
            return "Usage: /search <query>"
        try:
            res = await web_search(" ".join(args))
        except Exception as e:
            return f"Search failed: {e}"
        return "\n".join(f"- {r.title}\n    {r.url}\n    {r.snippet}" for r in res) or "No results."

    if cmd == "/learn":
        if args and args[0] in ("on", "off"):
            agent.auto_learn = args[0] == "on"
        return f"Automatic learning is {'on' if agent.auto_learn else 'off'}."

    if cmd == "/cancel":
        agent.cancel()
        return "Cancel requested."

    return f"Unknown command {cmd}. Try /help."
