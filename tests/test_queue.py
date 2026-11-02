import json
import os
import signal
import stat
import subprocess
import sys
import time
import uuid

import pytest

from tuicr_agent_runner.cli import main
from tuicr_agent_runner.core import RunnerError, write_json
from tuicr_agent_runner.queue import initialize_queue, queue_path

SUCCESS = """
import json
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                  'session_id': 'fresh-fake', 'permission_denials': []}))
"""


@pytest.fixture
def spool(tmp_path):
    queue = initialize_queue(tmp_path / "queue")
    launch = tmp_path / "starting directory not a repository"
    launch.mkdir()
    return queue, launch


def submit(queue, markdown="Synthetic review", batch=None):
    batch = batch or uuid.uuid4().hex
    write_json(
        queue / "pending" / f"{batch}.json", {"version": 1, "id": batch, "markdown": markdown}
    )
    return batch


def args_for(spool, claude, *extra):
    queue, launch = spool
    return [
        "watch",
        "--queue-dir",
        str(queue),
        "--launch-dir",
        str(launch),
        "--trust-config",
        "--claude",
        claude,
        "--poll-interval",
        "0.05",
        *extra,
    ]


def record(queue, batch):
    return json.loads((queue / "jobs" / batch / "job.json").read_text())


def wait_until(predicate, timeout=10):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            pytest.fail("Timed out waiting for fake subprocess")
        time.sleep(0.02)


def start_watch(args):
    return subprocess.Popen(
        [sys.executable, "-m", "tuicr_agent_runner", *args],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )


def stop_watch(child, sig=signal.SIGTERM):
    if child.poll() is None:
        child.send_signal(sig)
    return child.communicate(timeout=8)


def test_exact_markdown_custom_cwd_claim_before_launch_and_dedupe(
    spool, claude_factory, monkeypatch
):
    queue, launch = spool
    markdown = '# PR owner/repo#42\r\n\nCommit: abc123\n\n‘λ🐈’ e\u0301\t"quoted"\u0000\n'
    batch = submit(queue, markdown)
    fake = claude_factory(
        f"""
import json, os, pathlib, sys
job = json.loads(pathlib.Path({str(queue / "jobs" / batch / "job.json")!r}).read_text())
assert job['status'] == 'running'
assert pathlib.Path({str(queue / "jobs" / batch / "submission.json")!r}).exists()
assert os.getcwd() == {str(launch)!r}
assert 'GIT_DIR' not in os.environ
pathlib.Path('received.bin').write_bytes(sys.stdin.buffer.read())
pathlib.Path('argv.json').write_text(json.dumps(sys.argv[1:]))
with pathlib.Path('calls').open('a') as f: f.write('run\\n')
"""
        + SUCCESS
    )
    monkeypatch.setenv("GIT_DIR", "/a/nonexistent/unrelated/repository")
    args = args_for(spool, fake, "--once", "--allow-tool", "Bash(my-worktree-router*)")
    assert main(args) == 0
    assert (launch / "received.bin").read_bytes() == markdown.encode("utf-8")
    argv = json.loads((launch / "argv.json").read_text())
    assert argv == [
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
        "--allowedTools",
        "Bash(my-worktree-router*)",
    ]
    assert record(queue, batch)["status"] == "succeeded"
    assert record(queue, batch)["launch_dir"] == str(launch)
    assert not (queue / "pending" / f"{batch}.json").exists()
    submit(queue, "Different text must not bypass ID deduplication", batch)
    assert main(args) == 2
    assert (launch / "calls").read_text() == "run\n"
    assert len(list((queue / "rejected").iterdir())) == 1
    assert main(["jobs", "--queue-dir", str(queue)]) == 0


def test_only_explicit_submission_not_saved_comments_or_temporary_files(spool, claude_factory):
    queue, launch = spool
    fake = claude_factory()
    (launch / "session.json").write_text('{"comments": ["saved"]}')
    write_json(queue / "pending" / ".incomplete.tmp", {"markdown": "not published"})
    assert main(args_for(spool, fake, "--once")) == 0
    assert not (launch / "received.txt").exists()
    submit(queue, "Explicit send action")
    assert main(args_for(spool, fake, "--once")) == 0
    assert (launch / "received.txt").read_text() == "Explicit send action"


def test_queued_jobs_serialize(spool, claude_factory):
    queue, launch = spool
    first = submit(queue, "first")
    second = submit(queue, "second")
    fake = claude_factory(
        """
import os, pathlib, sys, time
prompt = sys.stdin.read()
fd = os.open('active', os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
with pathlib.Path('order').open('a') as f: f.write('start:' + prompt + '\\n')
time.sleep(0.1)
with pathlib.Path('order').open('a') as f: f.write('end:' + prompt + '\\n')
os.close(fd)
pathlib.Path('active').unlink()
"""
        + SUCCESS
    )
    assert main(args_for(spool, fake, "--once")) == 0
    assert (launch / "order").read_text().splitlines() == [
        "start:first",
        "end:first",
        "start:second",
        "end:second",
    ]
    assert all(record(queue, batch)["status"] == "succeeded" for batch in (first, second))


@pytest.mark.parametrize("sig", [signal.SIGINT, signal.SIGTERM])
def test_watcher_lock_and_cancellation_preserve_queued_work(spool, claude_factory, sig):
    queue, launch = spool
    first = submit(queue, "first")
    second = submit(queue, "second")
    marker = launch / "started"
    fake = claude_factory(
        f"import pathlib,time\npathlib.Path({str(marker)!r}).touch()\ntime.sleep(60)"
    )
    args = args_for(spool, fake)
    child = start_watch(args)
    try:
        wait_until(marker.exists)
        assert main([*args, "--once"]) == 2
        assert (
            main(["acknowledge-interrupted", first, "--queue-dir", str(queue), "--confirm-stopped"])
            == 2
        )
        stdout, stderr = stop_watch(child, sig)
        assert child.returncode == 130, (stdout, stderr)
        assert record(queue, first)["status"] == "cancelled"
        assert not (queue / "jobs" / second).exists()
        assert (queue / "pending" / f"{second}.json").exists()
    finally:
        stop_watch(child)


def test_idle_watcher_polls_new_submissions_and_stops_cleanly(spool, claude_factory):
    queue, launch = spool
    child = start_watch(args_for(spool, claude_factory()))
    try:
        wait_until(lambda: (queue / "watch.lock").exists())
        (launch / "session.json").write_text("saved comment, not an instruction to run")
        time.sleep(0.15)
        assert not (launch / "received.txt").exists()
        batch = submit(queue)
        wait_until(
            lambda: (
                (queue / "jobs" / batch / "job.json").exists()
                and record(queue, batch)["status"] == "succeeded"
            )
        )
        stdout, stderr = stop_watch(child)
        assert child.returncode == 130, (stdout, stderr)
    finally:
        stop_watch(child)


@pytest.mark.parametrize("state", [None, "claimed", "running", "unknown"])
def test_interrupted_claim_requires_explicit_reconciliation(spool, claude_factory, state):
    queue, launch = spool
    batch = uuid.uuid4().hex
    directory = queue / "jobs" / batch
    directory.mkdir(mode=0o700)
    if state:
        write_json(directory / "job.json", {"status": state, "id": batch})
    waiting = submit(queue)
    args = args_for(spool, claude_factory(), "--once")
    assert main(args) == 2
    assert not (launch / "received.txt").exists()
    recovery = ["acknowledge-interrupted", batch, "--queue-dir", str(queue)]
    assert main(recovery) == 2
    assert main([*recovery, "--confirm-stopped"]) == 0
    assert main(args) == 0
    assert record(queue, batch)["status"] == "interrupted"
    assert record(queue, waiting)["status"] == "succeeded"
    submit(queue, "Never rerun a consumed crash ID", batch)
    assert main(args) == 2
    assert main([*recovery, "--confirm-stopped"]) == 2


def test_sigkill_restart_blocks_while_orphan_fake_claude_survives(spool, claude_factory):
    queue, launch = spool
    first = submit(queue)
    second = submit(queue)
    marker = launch / "pid"
    fake = claude_factory(
        f"import os,pathlib,time\npathlib.Path({str(marker)!r}).write_text(str(os.getpid()))"
        "\ntime.sleep(60)"
    )
    args = args_for(spool, fake)
    child = start_watch(args)
    pid = None
    try:
        wait_until(marker.exists)
        pid = int(marker.read_text())
        child.kill()
        child.wait(timeout=5)
        os.kill(pid, 0)  # The fake Claude outlived the watcher, reproducing the hazard.
        assert main([*args, "--once"]) == 2
        assert record(queue, first)["status"] == "running"
        assert not (queue / "jobs" / second).exists()
    finally:
        if pid is not None:
            os.killpg(pid, signal.SIGKILL)
        stop_watch(child)


@pytest.mark.parametrize(
    ("body", "status", "exit_code"),
    [
        ("import time\ntime.sleep(60)", "timed_out", 124),
        (
            SUCCESS.replace(
                "'permission_denials': []", "'permission_denials': [{'tool_name': 'Bash'}]"
            ),
            "permission_denied",
            1,
        ),
        ("print('not JSON')", "failed", 1),
    ],
)
def test_watch_terminal_failure_no_retry(spool, claude_factory, body, status, exit_code):
    queue, _ = spool
    batch = submit(queue)
    args = args_for(spool, claude_factory(body), "--once", "--timeout", "0.2")
    assert main(args) == exit_code
    assert record(queue, batch)["status"] == status
    assert main(args) == 0  # Failure is terminal, not left pending for retry.
    submit(queue, batch=batch)
    assert main(args) == 2
    assert record(queue, batch)["status"] == status


@pytest.mark.parametrize(
    "payload",
    [
        b"{",
        b"\xff",
        b"[]",
        b'{"version":1}',
        b'{"version":true,"id":"ID","markdown":"text"}',
        b'{"version":1,"id":"wrong","markdown":"text"}',
        b'{"version":1,"id":"ID","markdown":42}',
        b'{"version":1,"id":"ID","markdown":""}',
        b'{"version":1,"id":"ID","markdown":"\\ud800"}',
    ],
)
def test_malformed_queue_is_recorded_without_launch(spool, claude_factory, payload):
    queue, launch = spool
    batch = uuid.uuid4().hex
    path = queue / "pending" / f"{batch}.json"
    path.write_bytes(payload.replace(b"ID", batch.encode()))
    path.chmod(0o600)
    assert main(args_for(spool, claude_factory(), "--once")) == 2
    assert record(queue, batch)["status"] == "rejected"
    assert not (launch / "received.txt").exists()


@pytest.mark.parametrize("kind", ["symlink", "fifo", "public", "oversize", "badname"])
def test_unsafe_queue_files_rejected(spool, claude_factory, kind):
    queue, launch = spool
    batch = uuid.uuid4().hex
    path = queue / "pending" / f"{batch}.json"
    if kind == "symlink":
        target = launch / "do-not-read"
        target.write_text("private")
        path.symlink_to(target)
    elif kind == "fifo":
        os.mkfifo(path, 0o600)
    elif kind == "public":
        submit(queue, batch=batch)
        path.chmod(0o644)
    elif kind == "oversize":
        with path.open("wb") as file:
            file.truncate(16 * 1024 * 1024 + 1)
        path.chmod(0o600)
    else:
        write_json(queue / "pending" / "invalid.json", {"markdown": "wrong name"})
    assert main(args_for(spool, claude_factory(), "--once")) == 2
    assert not (launch / "received.txt").exists()


def test_markdown_utf8_byte_size_bound(spool, claude_factory, monkeypatch):
    queue, _ = spool
    monkeypatch.setattr("tuicr_agent_runner.queue.MAX_MARKDOWN", 7)
    batch = submit(queue, "🐈🐈")  # 2 characters but 8 UTF-8 bytes.
    assert main(args_for(spool, claude_factory(), "--once")) == 2
    assert record(queue, batch)["status"] == "rejected"


@pytest.mark.parametrize("mode", [0o755, 0o777])
def test_private_queue_required(tmp_path, mode):
    path = tmp_path / "public-queue"
    path.mkdir(mode=mode)
    path.chmod(mode)
    with pytest.raises(RunnerError, match="private"):
        initialize_queue(path)
    assert stat.S_IMODE(path.stat().st_mode) == mode


def test_queue_path_defaults_and_override_precedence(tmp_path, monkeypatch):
    monkeypatch.delenv("TUICR_AGENT_QUEUE_DIR", raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    assert queue_path() == tmp_path / ".local/state/tuicr/agent-queue"
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    assert queue_path() == tmp_path / "state/tuicr/agent-queue"
    monkeypatch.setattr(sys, "platform", "darwin")
    assert queue_path() == tmp_path / "Library/Application Support/tuicr/agent-queue"
    monkeypatch.setenv("TUICR_AGENT_QUEUE_DIR", str(tmp_path / "override"))
    assert queue_path() == tmp_path / "override"
    assert queue_path(str(tmp_path / "flag")) == tmp_path / "flag"
    for invalid in ("", "relative/path", "~/not-expanded"):
        monkeypatch.setenv("TUICR_AGENT_QUEUE_DIR", invalid)
        with pytest.raises(RunnerError):
            queue_path()
    with pytest.raises(RunnerError):
        queue_path("/bad\0path")
    monkeypatch.delenv("TUICR_AGENT_QUEUE_DIR")
    monkeypatch.setattr(sys, "platform", "linux")
    for invalid in ("", "relative"):
        monkeypatch.setenv("XDG_STATE_HOME", invalid)
        with pytest.raises(RunnerError):
            queue_path()


def test_watch_validation_before_launch(spool, claude_factory):
    queue, launch = spool
    submit(queue)
    args = args_for(spool, claude_factory(), "--once")
    assert main([arg for arg in args if arg != "--trust-config"]) == 2
    for option in ("--timeout", "--poll-interval"):
        for value in ("0", "-1", "nan", "inf"):
            assert main([*args, option, value]) == 2
    for rule in ("", " ", "--resume", " --dangerously-skip-permissions"):
        assert main([*args, f"--allow-tool={rule}"]) == 2
    assert not (launch / "received.txt").exists()
    assert list((queue / "jobs").iterdir()) == []


def test_deeply_nested_json_is_rejected_not_crash_barrier(spool, claude_factory):
    queue, launch = spool
    batch = uuid.uuid4().hex
    path = queue / "pending" / f"{batch}.json"
    path.write_text("[" * 2000 + "0" + "]" * 2000)
    path.chmod(0o600)
    assert main(args_for(spool, claude_factory(), "--once")) == 2
    assert record(queue, batch)["status"] == "rejected"
    assert not (launch / "received.txt").exists()
    submit(queue)
    assert main(args_for(spool, claude_factory(), "--once")) == 0


def test_large_unicode_result_does_not_wedge_successful_queue(spool, claude_factory, capsys):
    queue, _ = spool
    batch = submit(queue)
    fake = claude_factory("""
import json
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                  'session_id': 'x' * 300, 'result': '🐈' * 1_400_000,
                  'permission_denials': []}, ensure_ascii=False))
""")
    args = args_for(spool, fake, "--once")
    assert main(args) == 0
    job = record(queue, batch)
    assert job["status"] == "succeeded"
    assert job["result_log"] == "stdout.jsonl"
    assert job["session_id_in_stdout_log"] is True
    assert (queue / "jobs" / batch / "job.json").stat().st_size < 2000
    final = json.loads((queue / "jobs" / batch / "stdout.jsonl").read_bytes())
    assert final["result"] == "🐈" * 1_400_000
    assert final["session_id"] == "x" * 300
    assert main(args) == 0  # A successful record must remain readable on restart.
    capsys.readouterr()


def test_jobs_includes_quarantined_duplicates_and_invalid_names(spool, claude_factory, capsys):
    queue, _ = spool
    batch = submit(queue)
    args = args_for(spool, claude_factory(), "--once")
    assert main(args) == 0
    submit(queue, batch=batch)
    write_json(queue / "pending" / "invalid.json", {})
    assert main(args) == 2
    capsys.readouterr()
    assert main(["jobs", "--queue-dir", str(queue)]) == 0
    jobs = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert len(jobs) == 3
    assert {job["status"] for job in jobs} == {"succeeded", "rejected"}
    assert {job.get("source") for job in jobs} == {None, f"{batch}.json", "invalid.json"}


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("home", [None, "", "relative"])
def test_default_requires_absolute_home_consistent_with_producer(monkeypatch, platform, home):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.delenv("TUICR_AGENT_QUEUE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    if home is None:
        monkeypatch.delenv("HOME", raising=False)
    else:
        monkeypatch.setenv("HOME", home)
    with pytest.raises(RunnerError, match="HOME"):
        queue_path()


def test_new_spool_parent_entries_are_synced(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr("tuicr_agent_runner.queue.sync_dir", synced.append)
    parent = tmp_path / "new-parent"
    queue = parent / "queue"
    initialize_queue(queue)
    assert tmp_path in synced and parent in synced and queue in synced
    for directory in (parent, queue, queue / "pending", queue / "jobs", queue / "rejected"):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        assert directory in synced and directory.parent in synced
