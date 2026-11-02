import io
import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from tuicr_agent_runner.cli import main
from tuicr_agent_runner.core import prepare, state_dir
from tuicr_agent_runner.process import run_process


def run_fake(project, claude_factory, body, timeout=10):
    directory = project["tmp"] / f"logs-{time.monotonic_ns()}"
    directory.mkdir()
    output = io.StringIO()
    result = run_process(
        [claude_factory(body)], project["repo"], "synthetic prompt", directory, timeout, output
    )
    return result, directory, output.getvalue()


def result_line(**overrides):
    event = {
        "type": "result",
        "subtype": "success",
        "is_error": False,
        "session_id": "fake-session",
        "result": "Done",
        "permission_denials": [],
    }
    event.update(overrides)
    return f"import json\nprint(json.dumps({event!r}))\n"


def test_success(project, claude_factory):
    result, directory, _ = run_fake(project, claude_factory, result_line())
    assert result["status"] == "succeeded"
    assert result["claude_session_id"] == "fake-session"
    assert result["exit_code"] == 0
    assert (directory / "stdout.jsonl").read_text()


@pytest.mark.parametrize(
    ("body", "status"),
    [
        ("print('not json')\n" + result_line(), "failed"),
        ("print('{}')", "failed"),
        (result_line(is_error=True), "failed"),
        (result_line(subtype="error_max_turns"), "failed"),
        (result_line(session_id=""), "failed"),
        (result_line(permission_denials=[{"tool_name": "Bash"}]), "permission_denied"),
        (result_line() + "raise SystemExit(7)", "failed"),
        ('import sys\nsys.stdout.write(\'{"type":"result"}\')', "failed"),
    ],
)
def test_failed_and_permission_outcomes(project, claude_factory, body, status):
    result, _, _ = run_fake(project, claude_factory, body)
    assert result["status"] == status
    assert result["error"]


def test_timeout_kills_group_including_stubborn_child(project, claude_factory):
    pid_file = project["tmp"] / "child.pid"
    body = f"""
import pathlib, subprocess, sys, time
code = 'import signal,time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(60)'
child = subprocess.Popen([sys.executable, '-c', code])
pathlib.Path({str(pid_file)!r}).write_text(str(child.pid))
time.sleep(60)
"""
    started = time.monotonic()
    result, _, _ = run_fake(project, claude_factory, body, timeout=0.5)
    assert result["status"] == "timed_out"
    assert time.monotonic() - started < 8
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        proc_stat = Path(f"/proc/{pid}/stat")
        if proc_stat.exists() and proc_stat.read_text().split()[2] == "Z":
            break
        time.sleep(0.05)
    else:
        pytest.fail("descendant was still running after timeout")


def test_drains_stdout_and_stderr(project, claude_factory):
    body = """
import json, sys
for _ in range(100):
    sys.stderr.write('x' * 65536)
    print(json.dumps({'type': 'system', 'padding': 'y' * 65536}))
""" + result_line()
    result, directory, output = run_fake(project, claude_factory, body)
    assert result["status"] == "succeeded"
    assert (directory / "stderr.log").stat().st_size > 6_000_000
    assert len(output) > 6_000_000


def test_missing_executable_is_recorded(project):
    directory = project["tmp"] / "logs"
    directory.mkdir()
    result = run_process(
        ["/nonexistent/synthetic-claude"], project["repo"], "x", directory, 1, io.StringIO()
    )
    assert result["status"] == "failed"
    assert result["error"]


def make_batch(project):
    return prepare(project["repo"], project["slug"], project["tuicr"], "origin", [])


def run_args(project, batch, claude):
    return [
        "run",
        "--repo",
        str(project["repo"]),
        batch["id"],
        "--approve",
        batch["approval"],
        "--trust-repo",
        "--claude",
        claude,
    ]


def jobs(project):
    return list((state_dir(project["repo"]) / "jobs").glob("*/job.json"))


def test_end_to_end_fresh_stdin_and_no_retry(project, claude_factory, capsys):
    original = project["session_path"].read_bytes()
    batch = make_batch(project)
    fake = claude_factory()
    args = run_args(project, batch, fake)
    assert main(args) == 0
    record = json.loads(jobs(project)[0].read_text())
    assert record["status"] == "succeeded"
    assert record["claude_session_id"] == "fake-session-123"
    argv = json.loads((project["repo"] / "argv.json").read_text())
    assert argv[:4] == ["-p", "--output-format", "stream-json", "--verbose"]
    assert "--resume" not in argv and "--continue" not in argv
    assert "--dangerously-skip-permissions" not in argv
    assert "dontAsk" in argv
    assert "mcp__*" in argv
    assert "comment-1" in (project["repo"] / "received.txt").read_text()
    assert project["session_path"].read_bytes() == original
    assert main(args) == 2
    assert "already attempted" in capsys.readouterr().err
    assert main(["jobs", "--repo", str(project["repo"])]) == 0


def test_approval_trust_and_option_injection_fail_before_child(project, claude_factory):
    batch = make_batch(project)
    fake = claude_factory()
    args = run_args(project, batch, fake)
    wrong = args.copy()
    wrong[wrong.index("--approve") + 1] = "wrong"
    assert main(wrong) == 2
    assert main([x for x in args if x != "--trust-repo"]) == 2
    for rule in ["--dangerously-skip-permissions", "--resume", "", " "]:
        assert main([*args, f"--allow-tool={rule}"]) == 2
    assert not jobs(project)
    assert not (project["repo"] / "received.txt").exists()


def test_setup_error_is_terminal_job(project, claude_factory, monkeypatch):
    batch = make_batch(project)

    def broken(*args):
        raise OSError("synthetic disk error")

    monkeypatch.setattr("tuicr_agent_runner.cli.run_process", broken)
    assert main(run_args(project, batch, claude_factory())) == 1
    assert json.loads(jobs(project)[0].read_text())["status"] == "failed"


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_cli_cancellation_and_worktree_overlap(project, claude_factory, sig):
    batch = make_batch(project)
    marker = project["tmp"] / "started"
    fake = claude_factory(
        f"import pathlib,time\npathlib.Path({str(marker)!r}).touch()\ntime.sleep(60)"
    )
    args = run_args(project, batch, fake)
    child = subprocess.Popen(
        [sys.executable, "-m", "tuicr_agent_runner", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 10
        while not marker.exists() and time.monotonic() < deadline:
            time.sleep(0.02)
        assert marker.exists()
        assert main(args) == 2
        child.send_signal(sig)
        stdout, stderr = child.communicate(timeout=8)
        assert child.returncode == 130, (stdout, stderr)
        assert json.loads(jobs(project)[0].read_text())["status"] == "cancelled"
    finally:
        if child.poll() is None:
            child.kill()
            child.wait()


def test_output_limits_kill_child(project, claude_factory, monkeypatch):
    monkeypatch.setattr("tuicr_agent_runner.process.MAX_LINE", 1024)
    result, _, _ = run_fake(project, claude_factory, "print('x' * 4096)")
    assert result["status"] == "failed"
    assert "oversized" in result["error"]
    monkeypatch.setattr("tuicr_agent_runner.process.MAX_OUTPUT", 1024)
    result, _, _ = run_fake(project, claude_factory, "import sys\nsys.stderr.write('x' * 4096)")
    assert result["status"] == "failed"
    assert "output limit" in result["error"]


def test_terminal_controls_removed(project, claude_factory):
    result, _, output = run_fake(
        project,
        claude_factory,
        "import sys\nsys.stderr.write('hello\\x1b[2Jworld')\n" + result_line(),
    )
    assert result["status"] == "succeeded"
    assert "\x1b" not in output
