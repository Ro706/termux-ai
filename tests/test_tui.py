import asyncio
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from termux_ai import app as app_mod
from termux_ai.config import Config
from termux_ai.ollama_client import ModelInfo, OllamaClient
from termux_ai.screens import ApprovalScreen


@pytest.fixture
def tui(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr("termux_ai.config.DATA_DIR", tmp_path / "data")
    monkeypatch.setattr("termux_ai.tools.DATA_DIR", tmp_path / "data")
    scripts = ['Making it.\n```action\n{"tool":"run","command":"touch made.txt"}\n```', "Created the file."]

    async def is_up(self): return True
    async def version(self): return "0.0-test"
    async def running(self): return []
    async def list_models(self): return [ModelInfo("fake:1b", 1_500_000_000, "1B", "Q4")]
    async def chat_stream(self, model, messages, temperature=0.4, num_ctx=4096):
        for i in range(0, len(scripts[0]), 9):
            yield scripts[0][i:i + 9]
        scripts.pop(0)
    async def chat(self, model, messages, **kw): return "{}"

    monkeypatch.setattr(OllamaClient, "is_up", is_up)
    monkeypatch.setattr(OllamaClient, "version", version)
    monkeypatch.setattr(OllamaClient, "running_models", running)
    monkeypatch.setattr(OllamaClient, "list_models", list_models)
    monkeypatch.setattr(OllamaClient, "chat_stream", chat_stream)
    monkeypatch.setattr(OllamaClient, "chat", chat)
    cfg = Config(workspace=str(tmp_path / "projects"), model="")
    (tmp_path / "projects" / "demo").mkdir(parents=True)
    a = app_mod.TermuxAI(cfg)
    a.executor.cwd = str(tmp_path)
    return a, tmp_path


def chat_text(app):
    return "\n".join(str(getattr(w, "renderable", getattr(w, "content", ""))) for w in app.chat.children)


def test_boot_and_slash(tui):
    app, tmp = tui

    async def go():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            assert app.agent.model == "fake:1b" and app.ollama_up
            await app._handle_input("/help")
            await pilot.pause(0.3)
            await app._handle_input("/memory add preference editor | I use nvim")
            await pilot.pause(0.3)
            assert app.mem.list_memories()[0].title == "editor" or app.mem.list_memories()[0].kind == "preference"
            await app._handle_input("!echo from-tui")
            await pilot.pause(0.8)
            assert "from-tui" in chat_text(app)
    asyncio.run(go())


def test_agent_approval_modal(tui):
    app, tmp = tui

    async def go():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            await app._handle_input("make a file")
            for _ in range(40):
                await pilot.pause(0.1)
                if isinstance(app.screen, ApprovalScreen):
                    break
            assert isinstance(app.screen, ApprovalScreen), "approval modal never appeared"
            assert not (tmp / "made.txt").exists()
            await pilot.press("y")
            for _ in range(40):
                await pilot.pause(0.1)
                if not app._busy:
                    break
            assert (tmp / "made.txt").exists()
    asyncio.run(go())


def test_all_screens_open_and_close(tui):
    app, tmp = tui

    async def go():
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause(0.5)
            for key in ("files", "monitor", "news", "projects", "memory", "settings"):
                depth = len(app.screen_stack)
                app.open_action(key)
                await pilot.pause(0.6)
                assert len(app.screen_stack) == depth + 1, f"{key} did not open"
                await pilot.press("escape")
                await pilot.pause(0.3)
                assert len(app.screen_stack) == depth, f"{key} did not close"
            await pilot.press("f1")
            await pilot.pause(0.3)
            assert len(app.screen_stack) == 2
            await pilot.press("escape")
    asyncio.run(go())


def test_narrow_layout(tui):
    app, tmp = tui

    async def go():
        async with app.run_test(size=(60, 30)) as pilot:
            await pilot.pause(0.5)
            assert app.screen.has_class("-narrow")
    asyncio.run(go())
