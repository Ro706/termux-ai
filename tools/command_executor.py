import asyncio, re

# Deliberately conservative guardrails. The user must approve every command.
BLOCK_PATTERNS = [
    r"\brm\s+-rf\s+(/|~|\$HOME)?\s*$",
    r"\bmkfs\b", r"\bdd\s+if=", r"\bshutdown\b", r"\breboot\b",
    r"\b:(){", r">\s*/dev/", r"\b su\b", r"\bsudo\b"
]

def validate_command(command: str):
    command = command.strip()
    if not command:
        return False, "Empty command."
    if any(re.search(p, command) for p in BLOCK_PATTERNS):
        return False, "Blocked by the built-in safety filter."
    return True, "Requires your approval."

async def run_command(command: str, timeout=30):
    ok, reason = validate_command(command)
    if not ok:
        return reason
    try:
        proc = await asyncio.create_subprocess_shell(
            command, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        text = out.decode(errors="replace")
        return text[-12000:] if text else f"(no output; exit code {proc.returncode})"
    except asyncio.TimeoutError:
        proc.kill()
        return f"Command timed out after {timeout} seconds."
    except Exception as exc:
        return f"Execution error: {exc}"
