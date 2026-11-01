"""Textual screens: approval modal, file manager, monitor, news, projects, memory, settings."""
from __future__ import annotations

import asyncio
import os
import shutil
import subprocess
import time
from pathlib import Path

from rich.markup import escape
from textual import on
from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.screen import ModalScreen, Screen
from textual.widgets import (
    Button, DataTable, DirectoryTree, Footer, Input, Label, OptionList, Static, Switch, TabbedContent, TabPane,
)
from textual.widgets.option_list import Option

from . import sysinfo, tools
from .agent import ApprovalRequest
from .config import save_config
from .news import NewsItem, fetch_category
from .security import Verdict


# --------------------------------------------------------------- approval
class ApprovalScreen(ModalScreen[bool]):
    BINDINGS = [Binding("y", "approve", "Approve"), Binding("n,escape", "deny", "Deny")]
    DEFAULT_CSS = """
    ApprovalScreen { align: center middle; }
    #approval-box { width: 90%; max-width: 100; height: auto; max-height: 90%; border: thick $warning; background: $surface; padding: 1 2; }
    #approval-box.dangerous { border: thick $error; }
    #approval-detail { height: auto; max-height: 16; overflow-y: auto; background: $boost; padding: 0 1; margin: 1 0; }
    #approval-buttons { height: auto; align-horizontal: right; }
    #approval-buttons Button { margin-left: 1; }
    #confirm-input { margin-bottom: 1; }
    """

    def __init__(self, req: ApprovalRequest):
        super().__init__()
        self.req = req
        self.dangerous = req.verdict is Verdict.DANGEROUS

    def compose(self) -> ComposeResult:
        with Vertical(id="approval-box", classes="dangerous" if self.dangerous else ""):
            colour = "red" if self.dangerous else "yellow"
            yield Label(f"[b {colour}]{'DANGEROUS - ' if self.dangerous else ''}{escape(self.req.title)}[/]")
            yield Label(f"[dim]{escape(self.req.reason)}[/]")
            yield Static(escape(self.req.detail), id="approval-detail")
            if self.dangerous:
                yield Input(placeholder="type YES to enable Approve", id="confirm-input")
            with Horizontal(id="approval-buttons"):
                yield Button("Deny (n)", id="deny", variant="default")
                yield Button("Approve (y)", id="approve", variant="error" if self.dangerous else "success", disabled=self.dangerous)

    @on(Input.Changed, "#confirm-input")
    def _check(self, ev: Input.Changed) -> None:
        self.query_one("#approve", Button).disabled = ev.value.strip() != "YES"

    @on(Button.Pressed, "#approve")
    def _approve_btn(self) -> None:
        self.action_approve()

    @on(Button.Pressed, "#deny")
    def _deny_btn(self) -> None:
        self.dismiss(False)

    def action_approve(self) -> None:
        if self.dangerous and self.query_one("#confirm-input", Input).value.strip() != "YES":
            self.query_one("#confirm-input", Input).focus()
            return
        self.dismiss(True)

    def action_deny(self) -> None:
        self.dismiss(False)


class TextPrompt(ModalScreen[str | None]):
    BINDINGS = [Binding("escape", "cancel", "Cancel")]
    DEFAULT_CSS = """
    TextPrompt { align: center middle; }
    #prompt-box { width: 90%; max-width: 80; height: auto; border: round $accent; background: $surface; padding: 1 2; }
    """

    def __init__(self, title: str, value: str = ""):
        super().__init__()
        self._title, self._value = title, value

    def compose(self) -> ComposeResult:
        with Vertical(id="prompt-box"):
            yield Label(self._title)
            yield Input(value=self._value, id="prompt-input")

    def on_mount(self) -> None:
        self.query_one(Input).focus()

    @on(Input.Submitted)
    def _done(self, ev: Input.Submitted) -> None:
        self.dismiss(ev.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ------------------------------------------------------------ file manager
class FileManagerScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("c", "cd_here", "cd here"),
        Binding("u", "go_up", "Parent dir"),
    ]
    DEFAULT_CSS = """
    FileManagerScreen #fm-tree { height: 55%; border: round $primary; }
    FileManagerScreen #fm-preview { height: 1fr; border: round $secondary; padding: 0 1; overflow-y: auto; }
    """

    def __init__(self):
        super().__init__()
        self.root = Path.cwd()
        self.selected: Path | None = None
        self.show_hidden = False

    def compose(self) -> ComposeResult:
        yield Label("", id="fm-title")
        yield DirectoryTree(str(self.root), id="fm-tree")
        with VerticalScroll(id="fm-preview"):
            yield Static("Select a file to preview. c = cd to selected folder, u = parent directory", id="fm-preview-text")
        yield Footer()

    def on_mount(self) -> None:
        self.root = Path(self.app.agent.exe.cwd)  # type: ignore[attr-defined]
        self._reload()

    def _reload(self) -> None:
        tree = self.query_one("#fm-tree", DirectoryTree)
        tree.path = str(self.root)
        tree.reload()
        self.query_one("#fm-title", Label).update(f"[b]{escape(str(self.root))}[/]")

    @on(DirectoryTree.FileSelected)
    def _file(self, ev: DirectoryTree.FileSelected) -> None:
        self.selected = Path(ev.path)
        try:
            text = tools.read_file(str(ev.path), str(self.root), max_bytes=6000)
        except tools.ToolError as e:
            text = f"({e})"
        self.query_one("#fm-preview-text", Static).update(escape(text))

    @on(DirectoryTree.DirectorySelected)
    def _dir(self, ev: DirectoryTree.DirectorySelected) -> None:
        self.selected = Path(ev.path)

    def action_cd_here(self) -> None:
        target = self.selected if self.selected and self.selected.is_dir() else (self.selected.parent if self.selected else self.root)
        self.app.agent.exe.chdir(str(target))  # type: ignore[attr-defined]
        self.app.agent.persist_context()  # type: ignore[attr-defined]
        self.root = target
        self._reload()
        self.app.notify(f"cwd: {target}")

    def action_go_up(self) -> None:
        self.root = self.root.parent
        self._reload()


# ----------------------------------------------------------------- monitor
class MonitorScreen(Screen):
    BINDINGS = [Binding("escape,q", "app.pop_screen", "Back"), Binding("r", "refresh_now", "Refresh")]
    DEFAULT_CSS = """
    MonitorScreen #mon-summary { height: auto; border: round $primary; padding: 0 1; }
    MonitorScreen #mon-procs { height: 1fr; }
    """

    def compose(self) -> ComposeResult:
        yield Static("loading...", id="mon-summary")
        yield DataTable(id="mon-procs", zebra_stripes=True)
        yield Footer()

    async def on_mount(self) -> None:
        t = self.query_one(DataTable)
        t.add_columns("PID", "Process", "CPU%", "MEM%")
        self.set_interval(3.0, self.action_refresh_now)
        await self.action_refresh_now()

    async def action_refresh_now(self) -> None:
        snap = await sysinfo.snapshot_async(self.app.agent.static)  # type: ignore[attr-defined]
        procs = await asyncio.to_thread(sysinfo.top_processes, 15)
        self.query_one("#mon-summary", Static).update(escape(snap.summary()))
        t = self.query_one(DataTable)
        t.clear()
        for pid, name, cpu, mem in procs:
            t.add_row(str(pid), name[:24], f"{cpu:.1f}", f"{mem:.1f}")


# -------------------------------------------------------------------- news
class NewsScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("r", "reload", "Reload"),
        Binding("o", "open_link", "Open link"),
        Binding("a", "ask_ai", "Ask AI to summarize"),
    ]
    DEFAULT_CSS = """
    NewsScreen OptionList { height: 1fr; }
    NewsScreen #news-detail { height: auto; max-height: 8; border: round $secondary; padding: 0 1; }
    """

    def __init__(self):
        super().__init__()
        self.items: dict[str, list[NewsItem]] = {}
        self.current: NewsItem | None = None

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="tech"):
            for cat in self.app.cfg.feeds:  # type: ignore[attr-defined]
                with TabPane(cat.title(), id=cat):
                    yield OptionList(id=f"list-{cat}")
        yield Static("Enter = details, o = open in browser, a = ask AI, r = reload", id="news-detail")
        yield Footer()

    async def on_mount(self) -> None:
        await self.action_reload()

    async def action_reload(self) -> None:
        self.query_one("#news-detail", Static).update("Fetching feeds...")
        for cat, urls in self.app.cfg.feeds.items():  # type: ignore[attr-defined]
            items = await fetch_category(urls, 25)
            self.items[cat] = items
            lst = self.query_one(f"#list-{cat}", OptionList)
            lst.clear_options()
            for i in items:
                age = ""
                if i.published:
                    hrs = max(0, int((time.time() - i.published) / 3600))
                    age = f" [dim]{hrs}h[/]" if hrs < 48 else f" [dim]{hrs // 24}d[/]"
                lst.add_option(Option(f"{escape(i.title[:90])}{age}"))
            if not items:
                lst.add_option(Option("[dim]no items (offline or feeds unreachable)[/]", disabled=True))
        self.query_one("#news-detail", Static).update("Loaded.")

    def _active_cat(self) -> str:
        return self.query_one(TabbedContent).active

    @on(OptionList.OptionHighlighted)
    @on(OptionList.OptionSelected)
    def _pick(self, ev) -> None:
        cat = self._active_cat()
        items = self.items.get(cat, [])
        if ev.option_index is not None and ev.option_index < len(items):
            self.current = items[ev.option_index]
            i = self.current
            self.query_one("#news-detail", Static).update(f"[b]{escape(i.title)}[/]\n[dim]{escape(i.source)}[/]\n{escape(i.summary)}\n[blue]{escape(i.link)}[/]")

    def action_open_link(self) -> None:
        if not self.current:
            return
        opener = shutil.which("termux-open-url") or shutil.which("xdg-open")
        if opener:
            subprocess.Popen([opener, self.current.link], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        else:
            self.app.notify(self.current.link, title="Link (no opener found)")

    def action_ask_ai(self) -> None:
        if not self.current:
            return
        i = self.current
        self.app.pop_screen()
        self.app.submit_text(f"Briefly explain why this headline matters: \"{i.title}\" ({i.source}). Context: {i.summary}")  # type: ignore[attr-defined]


# ---------------------------------------------------------------- projects
class ProjectScreen(Screen):
    BINDINGS = [Binding("escape,q", "app.pop_screen", "Back"), Binding("n", "new_project", "New (ask AI)")]
    DEFAULT_CSS = "ProjectScreen OptionList { height: 1fr; } ProjectScreen #proj-info { height: auto; padding: 0 1; }"

    def compose(self) -> ComposeResult:
        yield Static("", id="proj-info")
        yield OptionList(id="proj-list")
        yield Footer()

    def on_mount(self) -> None:
        self._load()

    def _load(self) -> None:
        ws = Path(self.app.cfg.workspace).expanduser()  # type: ignore[attr-defined]
        self.query_one("#proj-info", Static).update(f"Workspace: {escape(str(ws))}\nEnter = make current project + cd. n = ask the AI to scaffold a new one.")
        lst = self.query_one("#proj-list", OptionList)
        lst.clear_options()
        self._dirs: list[Path] = []
        if ws.is_dir():
            for d in sorted(p for p in ws.iterdir() if p.is_dir() and not p.name.startswith(".")):
                git = " [green]git[/]" if (d / ".git").exists() else ""
                self._dirs.append(d)
                lst.add_option(Option(f"{escape(d.name)}{git}"))
        if not self._dirs:
            lst.add_option(Option("[dim]no projects yet - press n[/]", disabled=True))

    @on(OptionList.OptionSelected)
    def _select(self, ev: OptionList.OptionSelected) -> None:
        if ev.option_index is None or ev.option_index >= len(self._dirs):
            return
        d = self._dirs[ev.option_index]
        agent = self.app.agent  # type: ignore[attr-defined]
        agent.exe.chdir(str(d))
        agent.set_project(d.name)
        agent.persist_context()
        self.app.notify(f"Project: {d.name}")
        self.app.pop_screen()

    async def action_new_project(self) -> None:
        name = await self.app.push_screen_wait(TextPrompt("New project name (the AI will propose the commands and ask approval):"))  # type: ignore[attr-defined]
        if name and name.strip():
            self.app.pop_screen()
            self.app.submit_text(f"Create a Python project called {name.strip()} inside {self.app.cfg.workspace}. Show the structure and explain the files.")  # type: ignore[attr-defined]


# ------------------------------------------------------------------ memory
class MemoryScreen(Screen):
    BINDINGS = [
        Binding("escape,q", "app.pop_screen", "Back"),
        Binding("d", "delete", "Delete"),
        Binding("e", "edit", "Edit"),
        Binding("x", "export", "Export"),
    ]
    DEFAULT_CSS = "MemoryScreen DataTable { height: 1fr; } MemoryScreen #mem-detail { height: auto; max-height: 8; border: round $secondary; padding: 0 1; }"

    def compose(self) -> ComposeResult:
        with TabbedContent(initial="mem-tab"):
            with TabPane("Memories", id="mem-tab"):
                yield DataTable(id="mem-table", cursor_type="row", zebra_stripes=True)
            with TabPane("Tasks", id="task-tab"):
                yield DataTable(id="task-table", cursor_type="row", zebra_stripes=True)
        yield Static("d = delete, e = edit body, x = export JSON", id="mem-detail")
        yield Footer()

    def on_mount(self) -> None:
        self.query_one("#mem-table", DataTable).add_columns("ID", "Kind", "Title", "Uses")
        self.query_one("#task-table", DataTable).add_columns("ID", "Status", "Title", "Next steps")
        self._load()

    def _load(self) -> None:
        mem = self.app.agent.mem  # type: ignore[attr-defined]
        mt = self.query_one("#mem-table", DataTable)
        mt.clear()
        self._mems = {str(m.id): m for m in mem.list_memories(limit=200)}
        for m in self._mems.values():
            mt.add_row(str(m.id), m.kind, m.title[:40], str(m.uses), key=str(m.id))
        tt = self.query_one("#task-table", DataTable)
        tt.clear()
        for t in mem.list_tasks(limit=100):
            tt.add_row(str(t.id), t.status, t.title[:40], (t.next_steps or "")[:40], key=str(t.id))

    @on(DataTable.RowHighlighted, "#mem-table")
    def _hl(self, ev: DataTable.RowHighlighted) -> None:
        m = self._mems.get(str(ev.row_key.value)) if ev.row_key else None
        if m:
            self.query_one("#mem-detail", Static).update(f"[b]{escape(m.title)}[/]\n{escape(m.body)}")

    def _current(self):
        t = self.query_one("#mem-table", DataTable)
        if t.row_count == 0:
            return None
        key = t.coordinate_to_cell_key(t.cursor_coordinate).row_key.value
        return self._mems.get(str(key))

    def action_delete(self) -> None:
        m = self._current()
        if m:
            self.app.agent.mem.delete_memory(m.id)  # type: ignore[attr-defined]
            self.app.notify(f"Deleted memory #{m.id}")
            self._load()

    async def action_edit(self) -> None:
        m = self._current()
        if not m:
            return
        new = await self.app.push_screen_wait(TextPrompt(f"Edit body of '{m.title}':", m.body))  # type: ignore[attr-defined]
        if new and new.strip():
            self.app.agent.mem.update_memory(m.id, body=new.strip())  # type: ignore[attr-defined]
            self._load()

    def action_export(self) -> None:
        dest = Path.home() / f"termux-ai-memory-{time.strftime('%Y%m%d-%H%M%S')}.json"
        self.app.agent.mem.export(dest)  # type: ignore[attr-defined]
        self.app.notify(f"Exported to {dest}")


# ---------------------------------------------------------------- settings
class SettingsScreen(Screen):
    BINDINGS = [Binding("escape,q", "app.pop_screen", "Back"), Binding("ctrl+s", "save", "Save")]
    DEFAULT_CSS = """
    SettingsScreen VerticalScroll { padding: 1 2; }
    SettingsScreen Label { margin-top: 1; }
    SettingsScreen #set-models { height: auto; max-height: 8; }
    """

    def compose(self) -> ComposeResult:
        cfg = self.app.cfg  # type: ignore[attr-defined]
        with VerticalScroll():
            yield Label("Ollama host")
            yield Input(cfg.ollama_host, id="set-host")
            yield Label("Model (pick below or type)")
            yield Input(self.app.agent.model, id="set-model")  # type: ignore[attr-defined]
            yield OptionList(id="set-models")
            yield Label("Temperature")
            yield Input(str(cfg.temperature), id="set-temp")
            yield Label("Command timeout (seconds)")
            yield Input(str(cfg.command_timeout), id="set-timeout")
            yield Label("Workspace directory")
            yield Input(cfg.workspace, id="set-workspace")
            yield Label("Automatic memory learning")
            yield Switch(self.app.agent.auto_learn, id="set-learn")  # type: ignore[attr-defined]
            yield Button("Save (ctrl+s)", id="set-save", variant="primary")
        yield Footer()

    async def on_mount(self) -> None:
        lst = self.query_one("#set-models", OptionList)
        try:
            for m in await self.app.agent.ollama.list_models():  # type: ignore[attr-defined]
                lst.add_option(Option(f"{m.name}  [dim]{m.size_human}[/]", id=m.name))
        except Exception:
            lst.add_option(Option("[dim]Ollama unreachable[/]", disabled=True))

    @on(OptionList.OptionSelected, "#set-models")
    def _pick(self, ev: OptionList.OptionSelected) -> None:
        if ev.option.id:
            self.query_one("#set-model", Input).value = ev.option.id

    @on(Button.Pressed, "#set-save")
    def _btn(self) -> None:
        self.action_save()

    def action_save(self) -> None:
        app = self.app
        cfg, agent = app.cfg, app.agent  # type: ignore[attr-defined]
        try:
            cfg.temperature = float(self.query_one("#set-temp", Input).value)
            cfg.command_timeout = int(self.query_one("#set-timeout", Input).value)
        except ValueError:
            app.notify("Temperature must be a number and timeout an integer", severity="error")
            return
        cfg.ollama_host = self.query_one("#set-host", Input).value.strip() or cfg.ollama_host
        cfg.workspace = self.query_one("#set-workspace", Input).value.strip() or cfg.workspace
        cfg.model = self.query_one("#set-model", Input).value.strip()
        agent.model = cfg.model or agent.model
        agent.exe.timeout = cfg.command_timeout
        agent.auto_learn = self.query_one("#set-learn", Switch).value
        save_config(cfg)
        agent.persist_context()
        app.notify("Settings saved. Host changes apply after restart.")
