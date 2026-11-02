"""One bounded POSIX subprocess; always drain both output streams."""

from __future__ import annotations

import json
import os
import selectors
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import TextIO

from .core import RunnerError, environment

MAX_OUTPUT = 64 * 1024 * 1024
MAX_LINE = 8 * 1024 * 1024


def display(text: str, output: TextIO) -> None:
    # Do not relay terminal-control sequences from model/repository content.
    safe = "".join(c if c in "\n\t" or (c.isprintable() and c != "\x1b") else "?" for c in text)
    output.write(safe)
    output.flush()


def stop_group(proc: subprocess.Popen) -> None:
    """Also kill descendants when the direct child has already exited."""
    try:
        os.killpg(proc.pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    try:
        proc.wait(timeout=2)
    except subprocess.TimeoutExpired:
        pass
    # The group can outlive its leader, so don't condition this on poll().
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass
    proc.wait(timeout=5)


def run_process(
    argv: list[str], repo: Path, prompt: str, log_dir: Path, timeout: float, output: TextIO
) -> dict:
    result = {
        "status": "failed",
        "exit_code": None,
        "claude_session_id": None,
        "result": None,
        "error": None,
    }
    final = None
    malformed = False
    buffer = b""
    total = 0
    proc = None
    started = time.monotonic()
    # A file as stdin prevents a large prompt from blocking before output draining.
    with (
        tempfile.TemporaryFile() as stdin,
        (log_dir / "stdout.jsonl").open("xb") as stdout_log,
        (log_dir / "stderr.log").open("xb") as stderr_log,
    ):
        os.chmod(log_dir / "stdout.jsonl", 0o600)
        os.chmod(log_dir / "stderr.log", 0o600)
        stdin.write(prompt.encode())
        stdin.seek(0)
        try:
            proc = subprocess.Popen(
                argv,
                cwd=repo,
                env=environment(),
                stdin=stdin,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            with selectors.DefaultSelector() as selector:
                selector.register(proc.stdout, selectors.EVENT_READ, "stdout")
                selector.register(proc.stderr, selectors.EVENT_READ, "stderr")
                while selector.get_map() or proc.poll() is None:
                    if time.monotonic() - started >= timeout:
                        result.update(status="timed_out", error="Claude exceeded the timeout.")
                        stop_group(proc)
                        break
                    for key, _ in selector.select(timeout=0.1):
                        chunk = os.read(key.fileobj.fileno(), 65536)
                        if not chunk:
                            selector.unregister(key.fileobj)
                            continue
                        total += len(chunk)
                        if total > MAX_OUTPUT:
                            raise RunnerError("Claude exceeded the 64 MiB output limit.")
                        if key.data == "stderr":
                            stderr_log.write(chunk)
                            display(chunk.decode(errors="replace"), output)
                            continue
                        stdout_log.write(chunk)
                        buffer += chunk
                        if len(buffer) > MAX_LINE:
                            raise RunnerError("Claude emitted an oversized stream-json record.")
                        while b"\n" in buffer:
                            line, buffer = buffer.split(b"\n", 1)
                            if not line.strip():
                                continue
                            try:
                                event = json.loads(line)
                                if not isinstance(event, dict):
                                    raise ValueError("not an object")
                            except (ValueError, UnicodeError):
                                malformed = True
                                continue
                            if event.get("type") == "result":
                                final = event
                            if event.get("type") == "assistant":
                                message = event.get("message", {})
                                blocks = (
                                    message.get("content", []) if isinstance(message, dict) else []
                                )
                                if isinstance(blocks, list):
                                    for block in blocks:
                                        if not isinstance(block, dict):
                                            continue
                                        if block.get("type") == "text":
                                            display(str(block.get("text", "")) + "\n", output)
                                        elif block.get("type") == "tool_use":
                                            display(f"[tool] {block.get('name', '?')}\n", output)
                if result["status"] == "timed_out":
                    result["exit_code"] = proc.returncode
                    return result
            result["exit_code"] = proc.wait(timeout=2)
            if buffer.strip():
                # NDJSON records must be newline terminated. Fail closed on truncation.
                malformed = True
            if final:
                result["claude_session_id"] = final.get("session_id")
                result["result"] = final
            if malformed:
                result["error"] = "Malformed or truncated Claude stream-json output."
            elif not final or (
                not isinstance(final.get("session_id"), str) or not final["session_id"]
            ):
                result["error"] = "Claude exited without a final result/session ID."
            elif final.get("permission_denials"):
                result.update(
                    status="permission_denied", error="Claude reported permission denials."
                )
            elif (
                result["exit_code"] != 0
                or final.get("is_error") is not False
                or final.get("subtype") != "success"
            ):
                result["error"] = "Claude reported an unsuccessful result; inspect the job logs."
            else:
                result["status"] = "succeeded"
        except KeyboardInterrupt:
            result.update(status="cancelled", error="Cancelled; partial edits may remain.")
        except (OSError, RunnerError, subprocess.TimeoutExpired) as exc:
            result["error"] = str(exc)
        finally:
            if proc is not None:
                stop_group(proc)
                result["exit_code"] = proc.returncode
                if proc.stdout:
                    proc.stdout.close()
                if proc.stderr:
                    proc.stderr.close()
    return result
