"""A private, durable spool for explicitly submitted tuicr Markdown batches."""

from __future__ import annotations

import contextlib
import json
import math
import os
import re
import signal
import stat
import sys
import time
import uuid
from pathlib import Path

from .core import RunnerError, now, write_json
from .process import display, run_process

MAX_ENVELOPE = 16 * 1024 * 1024
MAX_MARKDOWN = 8 * 1024 * 1024
BATCH_ID = re.compile(r"[0-9a-f]{32}")
TERMINAL = {
    "succeeded",
    "failed",
    "permission_denied",
    "timed_out",
    "cancelled",
    "interrupted",
    "rejected",
}


def queue_path(value: str | None = None) -> Path:
    if value is None:
        value = os.environ.get("TUICR_AGENT_QUEUE_DIR")
    if value is None:
        if sys.platform == "darwin":
            base = os.environ.get("HOME", "")
            if not base or not Path(base).is_absolute():
                raise RunnerError("HOME must be a nonempty absolute path.")
            value = str(Path(base) / "Library/Application Support/tuicr/agent-queue")
        elif os.name == "nt":
            base = os.environ.get("LOCALAPPDATA", "")
            if not base or not Path(base).is_absolute():
                raise RunnerError("LOCALAPPDATA must be an absolute path.")
            value = str(Path(base) / "tuicr/agent-queue")
        else:
            base = os.environ.get("XDG_STATE_HOME")
            if base is None:
                home = os.environ.get("HOME", "")
                if not home or not Path(home).is_absolute():
                    raise RunnerError("HOME must be a nonempty absolute path.")
                base = str(Path(home) / ".local/state")
            if not base or not Path(base).is_absolute():
                raise RunnerError("XDG_STATE_HOME must be a nonempty absolute path.")
            value = str(Path(base) / "tuicr/agent-queue")
    if not value or "\0" in value or not Path(value).is_absolute():
        raise RunnerError("Queue directory must be a nonempty absolute path.")
    return Path(value)


def sync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def create_dirs(path: Path) -> None:
    if path.exists():
        return
    create_dirs(path.parent)
    try:
        path.mkdir(mode=0o700)
    except FileExistsError:
        pass  # The producer may have created it concurrently; validation follows.
    sync_dir(path)
    sync_dir(path.parent)


def private_dir(path: Path) -> None:
    # Persist newly created ancestors too, so a first-use spool survives restart.
    create_dirs(path)
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
        raise RunnerError(
            f"Queue directory must be owned by you, private (0700), and not a symlink: {path}"
        )


def initialize_queue(path: Path) -> Path:
    if os.name != "posix":
        raise RunnerError("The watcher supports macOS/Linux only (POSIX locking/process groups).")
    private_dir(path)
    for name in ("pending", "jobs", "rejected"):
        private_dir(path / name)
    sync_dir(path)
    return path


def private_read(path: Path, limit: int = MAX_ENVELOPE) -> bytes:
    # O_NONBLOCK avoids hanging on a malformed FIFO; O_NOFOLLOW rejects symlinks.
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RunnerError(
                f"Queue file must be owned by you, private (0600), and regular: {path}"
            )
        if info.st_size > limit:
            raise RunnerError(f"Queue file exceeds {limit} bytes: {path}")
        raw = handle.read(limit + 1)
        if len(raw) > limit:
            raise RunnerError(f"Queue file exceeds {limit} bytes: {path}")
        return raw


def durable_record(path: Path, value: dict) -> None:
    write_json(path, value)
    sync_dir(path.parent)


@contextlib.contextmanager
def queue_locked(path: Path):
    import fcntl

    initialize_queue(path)
    fd = os.open(path / "watch.lock", os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    with os.fdopen(fd, "a") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o077:
            raise RunnerError("Queue lock must be a private regular file owned by you.")
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise RunnerError(
                "Another watcher is using this queue; only one can run at a time."
            ) from exc
        yield


def read_record(directory: Path) -> dict:
    private_dir(directory)
    try:
        job = json.loads(private_read(directory / "job.json"))
        if (
            not isinstance(job, dict)
            or not isinstance(job.get("status"), str)
            or job["status"] not in TERMINAL | {"claimed", "running"}
        ):
            raise ValueError("missing or unknown status")
    except (OSError, ValueError, RecursionError, RunnerError) as exc:
        raise RunnerError(
            f"Incomplete/invalid job record: {directory}. Inspect it and any orphaned Claude "
            "process before acknowledging the interrupted batch."
        ) from exc
    return job


def check_interrupted(path: Path) -> None:
    for directory in sorted((path / "jobs").iterdir()):
        job = read_record(directory)
        if job["status"] not in TERMINAL:
            raise RunnerError(
                f"Interrupted batch {directory.name} may have edited files or left Claude running. "
                "Stop any orphan, inspect its logs/edits, then use acknowledge-interrupted "
                f"{directory.name} --confirm-stopped. It will never be retried automatically."
            )


def load_submission(path: Path, batch_id: str) -> str:
    try:
        payload = json.loads(private_read(path).decode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise RunnerError("Submission must be UTF-8 JSON.") from exc
    if (
        not isinstance(payload, dict)
        or type(payload.get("version")) is not int
        or payload["version"] != 1
        or payload.get("id") != batch_id
        or not isinstance(payload.get("markdown"), str)
    ):
        raise RunnerError("Expected spool v1 with matching id and string markdown.")
    markdown = payload["markdown"]
    if not markdown or len(markdown.encode("utf-8")) > MAX_MARKDOWN:
        raise RunnerError("Markdown must be nonempty and at most 8 MiB UTF-8.")
    return markdown


def quarantine(path: Path, source: Path, reason: str) -> None:
    directory = path / "rejected" / uuid.uuid4().hex
    directory.mkdir(mode=0o700)
    source.rename(directory / "submission.json")
    durable_record(
        directory / "job.json", {"status": "rejected", "source": source.name, "error": reason}
    )
    sync_dir(path / "rejected")
    sync_dir(path / "pending")
    display(f"Rejected {source.name}: {reason}; record: {directory}\n", sys.stderr)


def process_submission(path: Path, source: Path, launch_dir: Path, args) -> str:
    batch_id = source.stem
    if not BATCH_ID.fullmatch(batch_id):
        quarantine(path, source, "Filename must be a 32-lowercase-hex batch ID plus .json")
        return "rejected"
    directory = path / "jobs" / batch_id
    try:
        directory.mkdir(mode=0o700)
    except FileExistsError:
        quarantine(path, source, "Batch ID was already claimed; no automatic retry")
        return "rejected"
    # A directory alone is a durable one-attempt marker. Any interrupted claim blocks
    # the whole queue on restart, even if it crashed before its first record write.
    sync_dir(path / "jobs")
    argv = [
        args.claude,
        "-p",
        "--output-format",
        "stream-json",
        "--verbose",
        "--permission-mode",
        "dontAsk",
    ]
    if args.allow_tool:
        argv.extend(["--allowedTools", *args.allow_tool])
    job = {
        "schema": 1,
        "id": batch_id,
        "batch": batch_id,
        "status": "claimed",
        "launch_dir": str(launch_dir),
        "started_at": now(),
        "argv": argv,
    }
    durable_record(directory / "job.json", job)
    source.rename(directory / "submission.json")
    sync_dir(directory)
    sync_dir(path / "pending")
    try:
        markdown = load_submission(directory / "submission.json", batch_id)
    except (OSError, ValueError, RecursionError, RunnerError) as exc:
        job.update(status="rejected", error=str(exc), finished_at=now())
        durable_record(directory / "job.json", job)
        display(f"Rejected {batch_id}: {exc}\n", sys.stderr)
        return "rejected"
    job["status"] = "running"
    durable_record(directory / "job.json", job)
    display(f"Batch {batch_id}; logs: {directory}\n", sys.stdout)
    try:
        outcome = run_process(argv, launch_dir, markdown, directory, args.timeout, sys.stdout)
    except KeyboardInterrupt:
        outcome = {"status": "cancelled", "error": "Cancelled before or after child execution."}
    except (OSError, ValueError, RecursionError, RunnerError) as exc:
        outcome = {"status": "failed", "error": str(exc)}
    # The full final event already lives in stdout.jsonl. Keep the durable status
    # bounded: JSON ASCII escaping can triple an otherwise valid stream record.
    summary = {key: value for key, value in outcome.items() if key != "result"}
    session_id = summary.get("claude_session_id")
    if isinstance(session_id, str) and len(session_id) > 256:
        summary.update(claude_session_id=None, session_id_in_stdout_log=True)
    job.update(summary, finished_at=now(), result_log="stdout.jsonl")
    durable_record(directory / "job.json", job)
    display(f"\n{job['status']}: {directory / 'job.json'}\n", sys.stdout)
    if job.get("error"):
        display(job["error"] + "\n", sys.stderr)
    if outcome.get("result") and isinstance(outcome["result"].get("result"), str):
        display(outcome["result"]["result"] + "\n", sys.stdout)
    return job["status"]


def watch(args) -> int:
    if not args.trust_config:
        raise RunnerError(
            "Pass --trust-config after reviewing your launch directory, Claude configuration, "
            "hooks/plugins/MCP, and queue writers. Headless Claude omits trust dialogs."
        )
    for name in ("timeout", "poll_interval"):
        if not math.isfinite(getattr(args, name)) or getattr(args, name) <= 0:
            raise RunnerError(f"--{name.replace('_', '-')} must be finite and positive.")
    if any(not rule.strip() or rule.lstrip().startswith("-") for rule in args.allow_tool):
        raise RunnerError("--allow-tool requires a nonempty tool rule, not a Claude option.")
    launch_dir = Path(args.launch_dir).expanduser().resolve(strict=True)
    if not launch_dir.is_dir():
        raise RunnerError("--launch-dir must be a directory (not necessarily a Git worktree).")
    path = queue_path(args.queue_dir)
    with queue_locked(path):
        check_interrupted(path)
        previous = signal.getsignal(signal.SIGTERM)

        def cancel(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, cancel)
        try:
            display(f"Watching {path / 'pending'}; Claude starts in {launch_dir}\n", sys.stdout)
            while True:
                # Poll only the small pending directory, never comments or historical jobs.
                pending = sorted(
                    (path / "pending").glob("*.json"), key=lambda p: (p.lstat().st_mtime_ns, p.name)
                )
                exit_code = 0
                for source in pending:
                    status = process_submission(path, source, launch_dir, args)
                    if status == "cancelled":
                        return 130
                    if status != "succeeded":
                        exit_code = {"timed_out": 124, "rejected": 2}.get(status, 1)
                if args.once:
                    return exit_code
                time.sleep(args.poll_interval)
        except KeyboardInterrupt:
            return 130
        finally:
            signal.signal(signal.SIGTERM, previous)


def acknowledge_interrupted(args) -> int:
    if not args.confirm_stopped:
        raise RunnerError(
            "Inspect partial edits and confirm no orphaned Claude remains; pass --confirm-stopped."
        )
    if not BATCH_ID.fullmatch(args.batch):
        raise RunnerError("Batch ID must be 32 lowercase hexadecimal characters.")
    path = queue_path(args.queue_dir)
    with queue_locked(path):
        directory = path / "jobs" / args.batch
        if not directory.exists():
            raise RunnerError("Unknown batch ID.")
        try:
            job = read_record(directory)
        except RunnerError:
            # A crash may have persisted the claim directory before the first record.
            private_dir(directory)
            job = {"schema": 1, "id": args.batch, "batch": args.batch}
        if job.get("status") in TERMINAL:
            raise RunnerError("This batch already has a terminal outcome; it will not be retried.")
        job.update(
            status="interrupted",
            finished_at=now(),
            error="Operator confirmed stopped; never retry this batch ID.",
        )
        durable_record(directory / "job.json", job)
    print(f"Acknowledged {args.batch} as interrupted; its ID remains permanently consumed.")
    return 0


def queue_jobs(value: str | None) -> None:
    path = initialize_queue(queue_path(value))
    for directory in sorted([*(path / "jobs").iterdir(), *(path / "rejected").iterdir()]):
        try:
            job = read_record(directory)
        except RunnerError as exc:
            job = {"id": directory.name, "status": "interrupted", "error": str(exc)}
        display(
            json.dumps({**job, "record": str(directory / "job.json")}, ensure_ascii=True) + "\n",
            sys.stdout,
        )
