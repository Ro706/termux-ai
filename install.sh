#!/data/data/com.termux/files/usr/bin/sh
# One-step setup for Termux AI: downloads Python and all required modules.
set -e
cd "$(dirname "$0")"

if command -v pkg >/dev/null 2>&1; then
  pkg update -y
  pkg install -y python git
  pkg install -y python-psutil termux-api 2>/dev/null || echo "(optional psutil/termux-api not installed; app still works)"
fi

# Termux forbids upgrading pip, so we only install the app and its modules
python -m pip install -e .

echo
echo "Done. Start with:  termux-ai   (or: python -m termux_ai)"
echo "Ollama must be running with a model pulled, e.g.:  ollama serve &  ollama pull qwen2.5:3b"
