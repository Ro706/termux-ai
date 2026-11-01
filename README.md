# Termux AI

A personal, local-first AI agent for Termux. Ollama does the thinking, Textual draws the dashboard, SQLite remembers.

## Install (Termux)

```bash
pkg update && pkg install python git
pkg install python-psutil        # optional; the app falls back to /proc and shell tools without it
pkg install termux-api           # optional, plus the Termux:API Android app, for battery/Wi-Fi info
termux-setup-storage             # optional, for ~/storage access

tar xzf termux-ai.tar.gz && cd termux_ai
sh install.sh                    # one step: downloads python + all modules (or: pip install -e .)
ollama serve &                   # if Ollama isn't already running
ollama pull qwen2.5:3b           # any model; small ones are best on a phone
termux-ai                        # or: python -m termux_ai
```

If any module is missing, `termux-ai` downloads it automatically on first run.

Ollama must be reachable at `http://127.0.0.1:11434` (override with `OLLAMA_HOST` or Settings). The first installed model is picked automatically; change it with `/model <name>`.

## Using it

Type normally and the AI answers. It can propose actions (run a command, read/write a file, search, fetch news, remember something) and you approve them.

| Input | What happens |
|---|---|
| `check my system health` | AI takes a live snapshot and summarises it |
| `!git status` | runs a command directly, through the same safety checks |
| `/help` | all slash commands |
| `/open files` (or F2) | file manager; also `monitor`, `news`, `projects`, `memory`, `settings`, `dash` |
| Esc | cancel the running generation or command |

Keys: F1 dashboard, F2 files, F3 monitor, F4 news, F5 projects, F6 memory, F7 settings, Ctrl+Q quit. Function keys can be awkward on a phone, so every screen is also reachable with `/open <name>`. On narrow screens (under 90 columns) the sidebar hides; use `/open dash` for the full dashboard.

### Memory commands

`/memory`, `/memory search <text>`, `/memory add <kind> <title> | <body>`, `/memory edit <id> <text>`, `/memory delete <id>`, `/memory clear [memories|history|tasks|commands|all] --yes`, `/memory export [path]`, `/history`, `/lasttask`, `/tasks`, `/resume`.

On restart the app restores your last working directory, project, model and conversation, and shows the last task and its next steps. After a turn that ran tools, the model is asked what is worth keeping (a fix that worked, a preference, project setup); that goes in as an inspectable, editable, deletable memory, not a transcript dump. Turn it off with `/learn off`.

## Safety model

Every command passes through `security.classify()` before you ever see a prompt.

- **BLOCKED, never runs even if you approve:** `sudo`/`su`/`tsu`, `rm -rf` on `/`, `~`, `$HOME`, `$PREFIX`, `/sdcard`, `*`; `mkfs`, partition tools, `dd` to devices, fork bombs, `curl | sh`, mount/reboot, reading SSH keys, `.env`, tokens or shell history.
- **DANGEROUS:** `rm`, `kill`, `ssh`/`scp`, `pkg remove`, `git push --force`/`reset --hard`/`clean`, `find -delete`. Needs approval and typing `YES`.
- **WRITE:** installs, `mkdir`/`mv`/`cp`, running scripts, redirects, unknown commands. Needs approval (press `y`).
- **READ-ONLY:** `ls`, `cat`, `git status`, `pip list`, etc. Runs without a prompt but is still shown and logged.

Other guarantees: 60 s timeout (configurable), Esc cancels the whole process group, every command is logged to `~/.local/share/termux-ai/commands.log`, secrets are redacted from command output, file reads and anything sent to the LLM, `.env`/key files can't be read or written by the agent, file writes are confined to your home and shared storage with the old version backed up, and the approval flow fails closed if no approver is attached.

## Layout

```
termux_ai/
  app.py          dashboard + chat TUI
  screens.py      approval modal, files, monitor, news, projects, memory, settings
  agent.py        prompt building, action protocol, approvals, learning
  security.py     command classification, redaction
  executor.py     subprocess runner: timeout, cancel, logging
  tools.py        file read/search/write with confinement and backups
  memory.py       SQLite (FTS5) memory, tasks, context
  sysinfo.py      Android/Termux metrics with fallbacks
  ollama_client.py  news.py  search.py  commands.py  config.py
tests/            pytest suite (security, memory, executor, agent loop, headless TUI)
```

Config lives at `~/.config/termux-ai/config.toml` (RSS feeds per category are editable there); data at `~/.local/share/termux-ai/`.

## Known limits

- Actions use a text protocol (fenced JSON blocks) rather than native tool calling, so it works with any Ollama model. Very small models sometimes emit malformed blocks; the agent reports them and carries on.
- Android 10+ often hides `/proc/stat`, so CPU% can fall back to load average. Battery and Wi-Fi need Termux:API.
- The command classifier is a safety net, not a sandbox. Read what you approve.
- Tested headless on Linux (39 tests), not yet on a physical device.
