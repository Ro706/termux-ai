# Termux AI — Personal Ollama Terminal Companion

A starter TUI assistant for Android Termux using Python, Textual, Ollama, and SQLite.

## Current MVP features
- Textual terminal dashboard and chat
- Ollama `/api/chat` streaming integration
- System snapshot (best-effort Android/Termux detection)
- SQLite conversation history and task log
- RSS news feed reader
- Explicit `/run COMMAND` workflow with a safety filter and approval dialog placeholder
- Configurable Ollama endpoint and model

## Important safety note
This is an early MVP. Do not expose the Ollama endpoint to untrusted networks. The command runner is intentionally limited but a regex filter is not a complete security boundary. Review commands and back up important files. Do not run this with elevated privileges.

## Install in Termux

```bash
pkg update && pkg upgrade
pkg install python git
termux-setup-storage
mkdir -p ~/projects
cd ~/projects
# Extract the project here, then:
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Install and run Ollama separately if supported in your chosen environment. If Ollama runs on another machine on your LAN, set `ollama_url` in `config/settings.json` to that machine's reachable address, for example `http://192.168.1.20:11434`.

Do not expose Ollama directly to the public internet. Use a trusted LAN or a secure tunnel.

## Run

```bash
python main.py
```

## Configuration
Edit `config/settings.json`:
- `ollama_url`: Ollama server URL
- `model`: installed model name
- `request_timeout`: generation timeout in seconds
- `max_history_messages`: recent chat context
- `monitor_refresh_seconds`: dashboard refresh interval
- `news_feeds`: RSS sources

## Commands
- `/help`
- `/system`
- `/news`
- `/lasttask`
- `/model gemma3:1b`
- `/run ls -la`
- `/clear`

## Known MVP limitations
- The command approval dialog and screen navigation need a follow-up hardening pass before treating command execution as production-ready.
- `psutil` metrics vary by Android version and may be unavailable.
- RSS feeds require internet access.
- Long-term memory currently stores conversation history and task records; semantic retrieval and project-specific memory are planned next.
- Some Termux API details require the separate Termux:API Android app and `termux-api` package.


## Animated dashboard widget
The dashboard includes a lightweight rotating ASCII globe and live device-local date/time. It uses Textual timers and does not require image assets or internet access.
