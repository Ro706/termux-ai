"""Main Textual application: dashboard + chat."""
from __future__ import annotations

import asyncio
import time

from rich.markdown import Markdown
from rich.markup import escape
from rich.panel import Panel
from rich.text import Text
from textual import on, work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll
from textual.screen import Screen
from textual.widgets import Footer, Input, ListItem, ListView, Static

from . import APP_NAME, __version__, commands, sysinfo
from .agent import Agent, ApprovalRequest, Hooks
from .config import Config, load_config
from .executor import Executor
from .memory import MemoryStore
from .ollama_client import OllamaClient, OllamaError
from .screens import (
    ApprovalScreen, FileManagerScreen, MemoryScreen, MonitorScreen, NewsScreen, ProjectScreen, SettingsScreen,
)

NARROW_COLS = 90

QUICK_ACTIONS = [
    ("chat", "Chat with AI"),
    ("command", "Execute command"),
    ("files", "File manager"),
    ("monitor", "System monitor"),
    ("news", "News & updates"),
    ("projects", "Project workspace"),
    ("memory", "Memory manager"),
    ("settings", "Settings"),
]


def _bar(pct: float | None, width: int = 14) -> str:
    if pct is None:
        return "[dim]" + "·" * width + "[/]"
    filled = int(round(min(max(pct, 0), 100) / 100 * width))
    colour = "green" if pct < 60 else "yellow" if pct < 85 else "red"
    return f"[{colour}]{'█' * filled}[/][dim]{'░' * (width - filled)}[/]"


class HeaderBar(Static):
    def on_mount(self) -> None:
        self.set_interval(1.0, self.refresh_view)
        self.refresh_view()

    def refresh_view(self) -> None:
        app: TermuxAI = self.app  # type: ignore[assignment]
        dot = "[green]●[/] online" if app.ollama_up else "[red]●[/] offline"
        status = app.agent.status
        colour = {"Idle": "green", "Thinking": "yellow", "Executing": "magenta"}.get(status, "cyan")
        self.update(
            f"[b cyan]◆ {APP_NAME}[/] [dim]v{__version__}[/]  [dim]│[/]  {time.strftime('%a %d %b %H:%M:%S')}  [dim]│[/]  "
            f"Ollama {dot}  [dim]│[/]  [b]{escape(app.agent.model or 'no model')}[/]  [dim]│[/]  [{colour}]{status}[/]"
        )


class SystemPanel(Static):
    def on_mount(self) -> None:
        self.border_title = "System"
        self.set_interval(1.0, self.refresh_view)
        self.refresh_view()

    def refresh_view(self) -> None:
        app: TermuxAI = self.app  # type: ignore[assignment]
        s = app.snapshot
        if s is None:
            self.update("[dim]collecting...[/]")
            return
        batt = f"{s.battery_percent}% {s.battery_status}" if s.battery_percent is not None else "n/a"
        pct = lambda v: f"{v:.0f}%" if v is not None else "n/a"
        self.update(
            f"[b]{escape(s.device)}[/]  Android {escape(s.android)}\n"
            f"[dim]Kernel[/] {escape(s.kernel[:26])}  [dim]Arch[/] {escape(s.arch)}\n"
            f"[dim]CPU[/]  {escape(s.cpu_model[:28])} x{s.cpu_cores}\n"
            f"      {_bar(s.cpu_percent)} {pct(s.cpu_percent)}\n"
            f"[dim]RAM[/]  {_bar(s.ram_percent)} {s.ram_used}/{s.ram_total}\n"
            f"      [dim]available {s.ram_available}[/]\n"
            f"[dim]Disk[/] {_bar(s.storage_percent)} {s.storage_used}/{s.storage_total}\n"
            f"[dim]Batt[/] {escape(batt)}\n"
            f"[dim]Net[/]  {escape(s.network)}\n"
            f"[dim]Termux[/] {escape(s.termux_version)}  "
            f"[dim]API[/] {'yes' if s.api_installed else 'no'}  [dim]Storage[/] {'yes' if s.storage_permission else 'no'}"
        )


class AIPanel(Static):
    def on_mount(self) -> None:
        self.border_title = "AI"
        self.set_interval(1.0, self.refresh_view)
        self.refresh_view()

    def refresh_view(self) -> None:
        app: TermuxAI = self.app  # type: ignore[assignment]
        a = app.agent
        last = time.strftime("%H:%M:%S", time.localtime(a.last_interaction)) if a.last_interaction else "never"
        gen = {"Thinking": "[yellow]generating[/]", "Executing": "[magenta]running command[/]"}.get(a.status, "[green]idle[/]")
        self.update(
            f"[dim]Model[/]   [b]{escape(a.model or 'none')}[/]\n"
            f"[dim]Size[/]    {app.model_size or 'n/a'}\n"
            f"[dim]Server[/]  {'[green]running[/]' if app.ollama_up else '[red]unreachable[/]'} {escape(app.ollama_version)}\n"
            f"[dim]Loaded[/]  {escape(', '.join(app.running_models) or 'none')}\n"
            f"[dim]State[/]   {gen}\n"
            f"[dim]Last[/]    {last}\n"
            f"[dim]Memory[/]  {escape(a.mem.status())}"
        )


class QuickActions(ListView):
    def compose(self) -> ComposeResult:
        for key, label in QUICK_ACTIONS:
            yield ListItem(Static(f" {label}"), name=key)

    def on_mount(self) -> None:
        self.border_title = "Quick actions"

    @on(ListView.Selected)
    def _selected(self, ev: ListView.Selected) -> None:
        self.app.open_action(ev.item.name or "chat")  # type: ignore[attr-defined]


class Dashboard(Vertical):
    def compose(self) -> ComposeResult:
        yield SystemPanel()
        yield AIPanel()
        yield QuickActions()


class ChatView(VerticalScroll):
    MAX_WIDGETS = 250

    def _add(self, widget: Static) -> Static:
        self.mount(widget)
        kids = list(self.children)
        if len(kids) > self.MAX_WIDGETS:
            kids[0].remove()
        self.scroll_end(animate=False)
        return widget

    def user(self, text: str) -> None:
        self._add(Static(Text.assemble(("you ▸ ", "bold cyan"), text), classes="msg-user"))

    def system(self, text: str) -> None:
        self._add(Static(Text(text, style="dim italic"), classes="msg-sys"))

    def assistant_start(self) -> Static:
        w = Static(Text("", no_wrap=False), classes="msg-ai")
        w.raw = ""  # type: ignore[attr-defined]
        return self._add(w)  # type: ignore[return-value]

    def assistant_append(self, w: Static, delta: str) -> None:
        w.raw += delta  # type: ignore[attr-defined]
        w.update(Text.assemble(("ai ▸ ", "bold green"), w.raw))
        self.scroll_end(animate=False)

    def assistant_finish(self, w: Static) -> None:
        raw = w.raw.strip()  # type: ignore[attr-defined]
        if not raw:
            w.remove()
            return
        w.update(Markdown(raw))
        self.scroll_end(animate=False)

    def tool(self, title: str, output: str, ok: bool) -> None:
        body = output if len(output) < 1800 else output[:900] + "\n…\n" + output[-800:]
        self._add(Static(Panel(Text(body or "(no output)"), title=Text(title[:70]), border_style="green" if ok else "red", title_align="left"), classes="msg-tool"))


class TermuxAI(App):
    TITLE = APP_NAME
    CSS = """
    Screen { background: $background; }
    HeaderBar { dock: top; height: 1; background: $panel; padding: 0 1; }
    #body { height: 1fr; }
    #sidebar { width: 40; border-right: tall $primary-darken-2; }
    SystemPanel { height: 12; border: round $primary; padding: 0 1; }
    AIPanel { height: 9; border: round $secondary; padding: 0 1; }
    QuickActions { height: 1fr; border: round $accent; }
    #chat-col { width: 1fr; }
    ChatView { height: 1fr; padding: 0 1; }
    .msg-user { margin-top: 1; }
    .msg-ai { margin-top: 1; }
    .msg-sys { margin-top: 1; }
    #prompt { dock: bottom; margin: 0 1; }
    Screen.-narrow #sidebar { display: none; }
    DashboardScreen Dashboard { height: 1fr; }
    """
    # Note: the focused Input already owns ctrl+d/f/x/c/v/k/w/u, so app-level shortcuts use
    # Escape + function keys; every screen is also reachable via `/open <name>` (Termux-friendly).
    BINDINGS = [
        Binding("ctrl+q", "quit", "Quit"),
        Binding("escape", "cancel", "Cancel"),
        Binding("f1", "dashboard", "Dash"),
        Binding("f2", "open('files')", "Files"),
        Binding("f3", "open('monitor')", "Monitor"),
        Binding("f4", "open('news')", "News"),
        Binding("f5", "open('projects')", "Projects"),
        Binding("f6", "open('memory')", "Memory"),
        Binding("f7", "open('settings')", "Settings"),
    ]

    def __init__(self, cfg: Config | None = None):
        super().__init__()
        self.cfg = cfg or load_config()
        self.ollama = OllamaClient(self.cfg.ollama_host, self.cfg.request_timeout)
        self.mem = MemoryStore(self.cfg.db_path)
        self.executor = Executor(self.cfg.command_log, self.cfg.command_timeout, self.cfg.max_output_chars)
        self.agent = Agent(self.cfg, self.mem, self.ollama, self.executor)
        self.snapshot: sysinfo.SystemSnapshot | None = None
        self.ollama_up = False
        self.ollama_version = ""
        self.running_models: list[str] = []
        self.model_size = ""
        self._busy = False

    # ----------------------------------------------------------- layout
    def compose(self) -> ComposeResult:
        yield HeaderBar()
        with Vertical(id="body"):
            pass
        yield Footer()

    async def on_mount(self) -> None:
        body = self.query_one("#body")
        from textual.containers import Horizontal
        row = Horizontal(id="row")
        await body.mount(row)
        await row.mount(Vertical(Dashboard(), id="sidebar"))
        chat_col = Vertical(id="chat-col")
        await row.mount(chat_col)
        await chat_col.mount(ChatView(id="chat"), Input(placeholder="Ask anything · /help · !command · /open dash · Esc cancels", id="prompt"))
        self._update_layout(self.size.width)
        self.query_one("#prompt", Input).focus()

        summary = self.agent.restore()
        chat = self.chat
        chat.system(f"{APP_NAME} v{__version__} - type /help for commands, !cmd to run a command, anything else goes to the AI.")
        if summary != "Nothing to resume yet.":
            chat.system("Restored last working context:\n" + summary)
        for r in self.mem.history(6):
            if r["content"].startswith("TOOL RESULTS:"):
                continue
        self.set_interval(self.cfg.refresh_seconds, self._poll_system)
        self.set_interval(5.0, self._poll_ollama)
        self._poll_system()
        await self._poll_ollama()
        if self.ollama_up and not self.agent.model:
            self.chat.system("No Ollama model installed yet. Run `ollama pull <model>` in another session.")

    @property
    def chat(self) -> ChatView:
        return self.query_one("#chat", ChatView)

    def on_resize(self, event) -> None:
        self._update_layout(event.size.width)

    def _update_layout(self, width: int) -> None:
        self.screen.set_class(width < NARROW_COLS, "-narrow")

    # ---------------------------------------------------------- polling
    @work(exclusive=True, group="sys")
    async def _poll_system(self) -> None:
        try:
            self.snapshot = await sysinfo.snapshot_async(self.agent.static)
        except Exception:
            pass  # keep the last good snapshot

    async def _poll_ollama(self) -> None:
        self.ollama_up = await self.ollama.is_up()
        if self.ollama_up:
            self.ollama_version = await self.ollama.version()
            self.running_models = await self.ollama.running_models()
            try:
                models = await self.ollama.list_models()
            except OllamaError:
                models = []
            if not self.agent.model:
                await self.agent.detect_model()
            cur = next((m for m in models if m.name == self.agent.model), None)
            self.model_size = (cur.size_human + (f" · {cur.parameter_size}" if cur.parameter_size else "")) if cur else ""

    # ---------------------------------------------------------- actions
    def open_action(self, key: str) -> None:
        self.call_later(self._open_action, key)

    async def _open_action(self, key: str) -> None:
        if isinstance(self.screen, DashboardScreen):
            self.pop_screen()
        screens = {
            "files": FileManagerScreen, "monitor": MonitorScreen, "news": NewsScreen,
            "projects": ProjectScreen, "memory": MemoryScreen, "settings": SettingsScreen,
        }
        if key in screens:
            await self.push_screen(screens[key]())
        elif key == "command":
            prompt = self.query_one("#prompt", Input)
            prompt.value = "!"
            prompt.focus()
            prompt.cursor_position = 1
        else:
            self.query_one("#prompt", Input).focus()

    def action_open(self, key: str) -> None:
        self.open_action(key)

    def action_dashboard(self) -> None:
        if isinstance(self.screen, DashboardScreen):
            self.pop_screen()
        else:
            self.push_screen(DashboardScreen())

    def action_clear_chat(self) -> None:
        self.chat.remove_children()

    def action_cancel(self) -> None:
        if len(self.screen_stack) > 1:
            return  # let the active screen's own escape binding handle it
        if self._busy:
            self.agent.cancel()
            self.notify("Cancelling...")

    def submit_text(self, text: str) -> None:
        inp = self.query_one("#prompt", Input)
        self.call_later(self._handle_input, text)

    # ------------------------------------------------------------ input
    @on(Input.Submitted, "#prompt")
    async def _submitted(self, ev: Input.Submitted) -> None:
        text = ev.value.strip()
        ev.input.value = ""
        if text:
            await self._handle_input(text)

    async def _handle_input(self, text: str) -> None:
        if text in ("/quit", "/exit"):
            self.exit()
            return
        if text == "/clear":
            self.action_clear_chat()
            return
        if text.split()[0] in ("/open", "/dash", "/settings", "/files", "/monitor", "/projects"):
            head, *rest = text.split()
            name = (rest[0] if head == "/open" and rest else head.lstrip("/")).lower()
            if name in ("dash", "dashboard"):
                self.call_later(self.action_dashboard)
            elif name in ("files", "monitor", "news", "projects", "memory", "settings", "command", "chat"):
                self.open_action(name)
            else:
                self.chat.system("Usage: /open files|monitor|news|projects|memory|settings|dash")
            return
        if self._busy and not text.startswith("/"):
            self.notify("Busy - press ctrl+x to cancel the current task.", severity="warning")
            return
        self.chat.user(text)
        if text.startswith("/"):
            self.run_worker(self._run_slash(text), exclusive=False)
        elif text.startswith("!"):
            self.run_worker(self._run_direct(text[1:].strip()), exclusive=False)
        else:
            self.run_worker(self._run_agent(text), exclusive=False)

    def _hooks(self) -> Hooks:
        chat = self.chat

        async def on_status(s: str) -> None:
            self.agent.status = s

        async def on_tool(title: str, output: str, ok: bool) -> None:
            chat.tool(title, output, ok)

        async def approve(req: ApprovalRequest) -> bool:
            return bool(await self.push_screen_wait(ApprovalScreen(req)))

        h = Hooks(on_status=on_status, on_tool=on_tool, approve=approve)
        return h

    async def _run_slash(self, text: str) -> None:
        try:
            out = await commands.handle(self.agent, text)
        except Exception as e:
            out = f"Command failed: {type(e).__name__}: {e}"
        if out:
            self.chat.system(out)

    async def _run_direct(self, command: str) -> None:
        if not command:
            return
        self._busy = True
        try:
            hooks = self._hooks()
            title, output, ok = await self.agent.run_command(command, hooks)
            self.chat.tool(title, output, ok)
        finally:
            self._busy = False
            self.agent.status = "Idle"

    async def _run_agent(self, text: str) -> None:
        self._busy = True
        chat = self.chat
        hooks = self._hooks()
        current = {"w": None}

        async def on_token(delta: str) -> None:
            if current["w"] is None:
                current["w"] = chat.assistant_start()
            chat.assistant_append(current["w"], delta)

        orig_tool = hooks.on_tool

        async def on_tool(title: str, output: str, ok: bool) -> None:
            if current["w"] is not None:
                chat.assistant_finish(current["w"])
                current["w"] = None
            await orig_tool(title, output, ok)

        hooks.on_token = on_token
        hooks.on_tool = on_tool
        try:
            await self.agent.respond(text, hooks)
        except Exception as e:
            chat.system(f"Agent error: {type(e).__name__}: {e}")
        finally:
            if current["w"] is not None:
                chat.assistant_finish(current["w"])
            self._busy = False
            self.agent.status = "Idle"

    async def on_unmount(self) -> None:
        try:
            self.agent.persist_context()
            await self.ollama.aclose()
            self.mem.close()
        except Exception:
            pass


class DashboardScreen(Screen):
    BINDINGS = [Binding("escape,q,f1", "app.pop_screen", "Back")]

    def compose(self) -> ComposeResult:
        yield HeaderBar()
        yield Dashboard()
        yield Footer()


def main() -> None:
    TermuxAI().run()


if __name__ == "__main__":
    main()
