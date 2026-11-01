import json
from pathlib import Path
from datetime import datetime
from textual.app import App, ComposeResult
from textual.containers import Horizontal, Vertical, ScrollableContainer
from textual.widgets import Header, Footer, Static, Input, Button, RichLog, Label
from textual.binding import Binding
from textual.screen import ModalScreen
from core.ollama_client import OllamaClient
from memory.database import MemoryDB
from system.info import system_snapshot
from tools.command_executor import validate_command, run_command
from news.fetcher import fetch_news

BASE = Path(__file__).resolve().parents[1]
SETTINGS = json.loads((BASE / "config" / "settings.json").read_text())

SYSTEM_PROMPT = """You are Termux AI, a careful personal assistant running in Android Termux.
Help with shell, Python, development, and system troubleshooting. Be concise and practical.
You do not have direct tool access unless the application explicitly provides it.
Never claim a command was executed unless the user approved it and the app returned its output.
Prefer safe, reversible steps. Warn before destructive or security-sensitive actions."""

class TermuxAIApp(App):
    TITLE = "TERMUX AI"
    SUB_TITLE = "Local Ollama Terminal Companion"
    CSS_PATH = str(Path(__file__).with_name("styles.tcss"))
    BINDINGS = [
        Binding("ctrl+c", "quit", "Quit"),
        Binding("ctrl+l", "clear_chat", "Clear chat"),
        Binding("f1", "dashboard", "Dashboard"),
        Binding("f2", "chat_focus", "Chat"),
    ]

    def __init__(self):
        super().__init__()
        self.db = MemoryDB()
        self.client = OllamaClient(SETTINGS["ollama_url"], SETTINGS["request_timeout"])
        self.model = SETTINGS["model"]
        self.busy = False

    def compose(self) -> ComposeResult:
        yield Header()
        with Horizontal(id="main"):
            with Vertical(id="sidebar"):
                yield Static("[b cyan]TERMUX AI[/b cyan]\n[dim]Personal agent[/dim]", id="brand")
                yield Button("Dashboard  [F1]", id="dashboard")
                yield Button("Chat  [F2]", id="chat")
                yield Button("System info", id="system")
                yield Button("News", id="news")
                yield Button("Memory / Tasks", id="memory")
                yield Static("", id="connection")
                yield Static("Commands:\n/help\n/system\n/news\n/lasttask\n/run COMMAND\n/model NAME", id="help")
            with Vertical(id="content"):
                yield Static("Loading system status…", id="status")
                with ScrollableContainer(id="chat_area"):
                    yield RichLog(id="chat_log", wrap=True, markup=True, auto_scroll=True)
                with Horizontal(id="composer"):
                    yield Input(placeholder="Ask Termux AI or enter a slash command…", id="prompt")
                    yield Button("Send", id="send", variant="primary")
        yield Footer()

    async def on_mount(self):
        self.query_one("#chat_log", RichLog).write("[bold cyan]Termux AI ready.[/bold cyan]")
        self.query_one("#chat_log", RichLog).write("Connect Ollama, then ask a question. Type /help for commands.")
        self.refresh_status()
        self.set_interval(SETTINGS["monitor_refresh_seconds"], self.refresh_status)

    def refresh_status(self):
        snap = system_snapshot()
        summary = "  •  ".join([f"{k}: {v}" for k, v in snap.items() if k in ("Device","Android","Architecture","CPU usage","Memory","Storage")])
        self.query_one("#status", Static).update(f"[b]SYSTEM[/b]  {summary}")
        self.query_one("#connection", Static).update(f"[dim]Ollama: {SETTINGS['ollama_url']}\\nModel: {self.model}[/dim]")

    async def on_button_pressed(self, event: Button.Pressed):
        bid = event.button.id
        if bid == "send":
            await self.submit_prompt()
        elif bid == "dashboard":
            self.show_dashboard()
        elif bid == "chat":
            self.query_one("#prompt", Input).focus()
        elif bid == "system":
            self.show_system()
        elif bid == "news":
            await self.show_news()
        elif bid == "memory":
            self.show_memory()

    async def on_input_submitted(self, event: Input.Submitted):
        if event.input.id == "prompt":
            await self.submit_prompt()

    async def submit_prompt(self):
        field = self.query_one("#prompt", Input)
        prompt = field.value.strip()
        if not prompt or self.busy:
            return
        field.value = ""
        log = self.query_one("#chat_log", RichLog)
        log.write(f"\\n[bold green]You[/bold green]: {prompt}")
        if prompt.startswith("/"):
            await self.handle_command(prompt)
            return
        self.busy = True
        self.db.add_message("user", prompt)
        try:
            messages = [{"role": "system", "content": SYSTEM_PROMPT}]
            messages += self.db.recent_messages(SETTINGS["max_history_messages"])
            log.write("[bold cyan]Termux AI[/bold cyan]:")
            parts = []
            def token(t):
                parts.append(t)
                log.write(t)
            await self.client.chat(self.model, messages, on_token=token)
            answer = "".join(parts)
            self.db.add_message("assistant", answer)
            log.write("")
        except Exception as exc:
            log.write(f"[bold red]Ollama error:[/bold red] {exc}")
            log.write("Check that Ollama is running and the configured model is installed.")
        finally:
            self.busy = False

    async def handle_command(self, command):
        log = self.query_one("#chat_log", RichLog)
        parts = command.split(maxsplit=1)
        name = parts[0].lower()
        arg = parts[1] if len(parts) > 1 else ""
        if name == "/help":
            log.write("Commands: /help /system /news /lasttask /run COMMAND /model NAME /clear")
        elif name == "/system":
            self.show_system()
        elif name == "/news":
            await self.show_news()
        elif name == "/lasttask":
            tasks = self.db.recent_tasks(1)
            log.write(str(tasks[0]) if tasks else "No saved tasks yet.")
        elif name == "/model" and arg:
            self.model = arg
            log.write(f"Model set to {self.model}. Ensure it exists in Ollama.")
            self.refresh_status()
        elif name == "/run" and arg:
            ok, reason = validate_command(arg)
            if not ok:
                log.write(f"[red]{reason}[/red]")
                return
            self.pending_command = arg
            self.push_screen(CommandApproval(arg), callback=self.command_approved)
        elif name == "/clear":
            log.clear()
        else:
            log.write("Unknown command. Type /help.")

    def command_approved(self, approved):
        if approved:
            command = self.pending_command
            self.run_worker(self._execute(command), exclusive=True)

    async def _execute(self, command):
        log = self.query_one("#chat_log", RichLog)
        log.write(f"[yellow]Executing:[/yellow] {command}")
        output = await run_command(command)
        log.write(output or "(no output)")
        self.db.add_task(f"Command: {command}", output[:1500])

    def show_dashboard(self):
        self.show_system()

    def show_system(self):
        log = self.query_one("#chat_log", RichLog)
        log.write("\\n[bold cyan]SYSTEM DETAILS[/bold cyan]")
        for key, value in system_snapshot().items():
            log.write(f"[bold]{key}:[/bold] {value}")

    async def show_news(self):
        log = self.query_one("#chat_log", RichLog)
        log.write("\\n[bold cyan]LATEST FEED UPDATES[/bold cyan] (RSS; requires internet)")
        entries = await fetch_news(SETTINGS["news_feeds"])
        if not entries:
            log.write("No feeds available. Check internet connectivity or RSS URLs.")
        for item in entries:
            log.write(f"[bold]{item['title']}[/bold]\\n[dim]{item['source']} • {item['date']}\\n{item['link']}[/dim]\\n")

    def show_memory(self):
        log = self.query_one("#chat_log", RichLog)
        log.write("\\n[bold cyan]RECENT TASK MEMORY[/bold cyan]")
        tasks = self.db.recent_tasks()
        if not tasks:
            log.write("No tasks saved yet.")
        for task in tasks:
            log.write(f"#{task[0]} [{task[2]}] {task[1]}\\n{task[3][:300]}")

    def action_clear_chat(self):
        self.query_one("#chat_log", RichLog).clear()

    def action_dashboard(self):
        self.show_dashboard()

    def action_chat_focus(self):
        self.query_one("#prompt", Input).focus()

class CommandApproval(ModalScreen):
    CSS = """
    CommandApproval { align: center middle; background: $background 60%; }
    #approval-box { width: 85%; height: auto; max-height: 15; border: round $warning; background: $panel; padding: 1 2; }
    #approval-buttons { height: 3; align: center middle; }
    #approval-buttons Button { margin: 0 1; }
    """
    def __init__(self, command):
        super().__init__()
        self.command = command

    def compose(self):
        with Vertical(id="approval-box"):
            yield Static("[bold yellow]Approve terminal command?[/bold yellow]")
            yield Static(self.command)
            yield Static("Only approve commands you understand.")
            with Horizontal(id="approval-buttons"):
                yield Button("Approve", id="approve", variant="warning")
                yield Button("Cancel", id="cancel")

    def on_button_pressed(self, event):
        self.dismiss(event.button.id == "approve")
