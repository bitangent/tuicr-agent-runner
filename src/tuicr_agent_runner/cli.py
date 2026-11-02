"""Explicit queued delivery, plus the original prepare → inspect → run workflow."""

from __future__ import annotations

import argparse
import json
import math
import signal
import sys
import uuid
from pathlib import Path

from .core import (
    RunnerError,
    check_current,
    load_batch,
    locked,
    now,
    prepare,
    prompt,
    read_json,
    repository,
    state_dir,
    write_json,
)
from .process import display, run_process
from .queue import acknowledge_interrupted, queue_jobs, watch

BUILTIN_TOOLS = "Read,Glob,Grep,Edit,Write,Bash"
DEFAULT_APPROVALS = ["Read", "Glob", "Grep", "Edit", "Write"]


def parser() -> argparse.ArgumentParser:
    root = argparse.ArgumentParser(
        description="Watch explicit tuicr submissions; launch fresh Claude one batch at a time."
    )
    root.add_argument("--version", action="version", version="tuicr-agent-runner 0.1.0")
    commands = root.add_subparsers(dest="command", required=True)
    prep = commands.add_parser(
        "prepare", help="Snapshot selected local-draft comments and checkout"
    )
    prep.add_argument(
        "--session", required=True, help="Exact tuicr slug or listed session JSON path"
    )
    prep.add_argument("--tuicr", default="tuicr", help="tuicr executable (not a shell command)")
    prep.add_argument(
        "--remote", default="origin", help="GitHub PR identity remote (default: origin)"
    )
    prep.add_argument(
        "--comment", action="append", default=[], help="Select comment ID; repeatable"
    )
    show = commands.add_parser("show", help="Inspect the exact saved batch before approval")
    show.add_argument("batch")
    run = commands.add_parser("run", help="Run one reviewed batch in its exact worktree")
    run.add_argument("batch")
    run.add_argument("--approve", required=True, help="Full SHA-256 from the inspected batch")
    run.add_argument(
        "--trust-repo", action="store_true", help="Acknowledge inherited Claude config/hooks"
    )
    run.add_argument("--claude", default="claude", help="Claude executable (not a shell command)")
    run.add_argument("--timeout", type=float, default=900, help="Wall-clock seconds (default: 900)")
    run.add_argument(
        "--allow-tool",
        action="append",
        default=[],
        help="Additional Claude preapproval, e.g. 'Bash(uv run pytest*)'; repeatable",
    )
    watcher = commands.add_parser(
        "watch", help="Watch explicit tuicr submissions; launch fresh Claude serially"
    )
    watcher.add_argument(
        "--queue-dir", help="Absolute spool path (or TUICR_AGENT_QUEUE_DIR/platform default)"
    )
    watcher.add_argument(
        "--launch-dir",
        required=True,
        help="Claude starting directory; your workflow chooses its worktree",
    )
    watcher.add_argument(
        "--trust-config",
        action="store_true",
        help="Acknowledge inherited Claude configuration and queue writers",
    )
    watcher.add_argument(
        "--claude", default="claude", help="Claude executable, not a shell command"
    )
    watcher.add_argument(
        "--timeout", type=float, default=900, help="Per-batch wall-clock seconds (default: 900)"
    )
    watcher.add_argument(
        "--poll-interval", type=float, default=1, help="Directory polling seconds (default: 1)"
    )
    watcher.add_argument(
        "--allow-tool",
        action="append",
        default=[],
        help="Additional explicit Claude preapproval; repeatable",
    )
    watcher.add_argument(
        "--once", action="store_true", help="Process the current queue snapshot, then exit"
    )
    acknowledge = commands.add_parser(
        "acknowledge-interrupted",
        help="After inspection, mark a crashed attempt stopped; never rerun it",
    )
    acknowledge.add_argument("batch")
    acknowledge.add_argument("--queue-dir")
    acknowledge.add_argument(
        "--confirm-stopped",
        action="store_true",
        help="Confirm no orphaned Claude remains and partial edits were inspected",
    )
    jobs = commands.add_parser(
        "jobs", help="List queue job outcomes/log paths (or legacy worktree jobs)"
    )
    source = jobs.add_mutually_exclusive_group()
    source.add_argument("--queue-dir", help="Queue path (defaults to env/platform default)")
    source.add_argument("--repo", help="List legacy worktree job records instead")
    for sub in (prep, show, run):
        sub.add_argument("--repo", required=True, help="Explicit Git worktree root")
    return root


def show_batch(batch: dict) -> None:
    print(json.dumps(batch, indent=2, ensure_ascii=True))
    print("\nInspect the checkout diff too. This snapshot does not prove old comments are fresh.")
    print("If this is the intended reviewed code and feedback, run:")
    # Repo path can contain shell syntax; don't generate an unsafe copy/paste command.
    import shlex

    print(
        f"tuicr-agent-runner run --repo {shlex.quote(batch['repo'])} {batch['id']} "
        f"--approve {batch['approval']} --trust-repo"
    )


def execute(repo: Path, args: argparse.Namespace) -> int:
    if not args.trust_repo:
        raise RunnerError(
            "Read the trusted-repository warning in README, then pass --trust-repo. "
            "Plain claude -p loads local/user hooks and config without trust dialogs."
        )
    if not math.isfinite(args.timeout) or args.timeout <= 0:
        raise RunnerError("--timeout must be finite and positive.")
    if any(not rule.strip() or rule.startswith("-") for rule in args.allow_tool):
        raise RunnerError("--allow-tool requires a nonempty tool rule, not a Claude option.")
    with locked(repo) as state:
        batch = load_batch(repo, args.batch)
        if args.approve != batch["approval"]:
            raise RunnerError("Approval does not match this batch; inspect it with show first.")
        if (state / "batches" / f"{args.batch}.attempted").exists():
            raise RunnerError(
                "This batch was already attempted. Inspect any partial edits and "
                "prepare a new batch; automatic retries are intentionally disabled."
            )
        check_current(repo, batch)
        job_id = uuid.uuid4().hex
        job_dir = state / "jobs" / job_id
        job_dir.mkdir(mode=0o700)
        argv = [
            args.claude,
            "-p",
            "--output-format",
            "stream-json",
            "--verbose",
            "--permission-mode",
            "dontAsk",
            "--tools",
            BUILTIN_TOOLS,
            "--disallowedTools",
            "mcp__*",
            "--allowedTools",
            *DEFAULT_APPROVALS,
            *args.allow_tool,
        ]
        job = {
            "schema": 1,
            "id": job_id,
            "batch": batch["id"],
            "approval": args.approve,
            "repo": str(repo),
            "started_at": now(),
            "status": "running",
            "argv": argv,
        }
        write_json(job_dir / "job.json", job)
        write_json(state / "batches" / f"{args.batch}.attempted", {"job": job_id})
        print(f"Job {job_id}; logs: {job_dir}", flush=True)
        previous = signal.getsignal(signal.SIGTERM)

        def cancel(signum, frame):
            raise KeyboardInterrupt

        signal.signal(signal.SIGTERM, cancel)
        try:
            outcome = run_process(argv, repo, prompt(batch), job_dir, args.timeout, sys.stdout)
        except KeyboardInterrupt:
            outcome = {"status": "cancelled", "error": "Cancelled before or after child execution."}
        except (OSError, ValueError, RunnerError) as exc:
            outcome = {"status": "failed", "error": str(exc)}
        finally:
            signal.signal(signal.SIGTERM, previous)
        job.update(outcome, finished_at=now())
        write_json(job_dir / "job.json", job)
        print(f"\n{job['status']}: {job_dir / 'job.json'}")
        if job.get("error"):
            display(job["error"] + "\n", sys.stderr)
        if job.get("result") and isinstance(job["result"].get("result"), str):
            display(job["result"]["result"] + "\n", sys.stdout)
        print(
            "Next: inspect git diff and tests; refresh/reopen the intended review in tuicr. "
            "Comments were not changed or marked addressed. Partial edits may remain on failure."
        )
        return {"succeeded": 0, "cancelled": 130, "timed_out": 124}.get(job["status"], 1)


def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    try:
        if args.command == "watch":
            return watch(args)
        if args.command == "acknowledge-interrupted":
            return acknowledge_interrupted(args)
        if args.command == "jobs" and not args.repo:
            queue_jobs(args.queue_dir)
            return 0
        repo = repository(args.repo)
        if args.command == "prepare":
            show_batch(prepare(repo, args.session, args.tuicr, args.remote, args.comment))
        elif args.command == "show":
            show_batch(load_batch(repo, args.batch))
        elif args.command == "run":
            return execute(repo, args)
        else:
            for path in sorted((state_dir(repo) / "jobs").glob("*/job.json")):
                job = read_json(path)
                print(
                    json.dumps(
                        {
                            "id": job["id"],
                            "batch": job["batch"],
                            "status": job["status"],
                            "started_at": job["started_at"],
                            "record": str(path),
                        }
                    )
                )
        return 0
    except (RunnerError, OSError, ValueError, KeyError, TypeError) as exc:
        display(f"error: {exc}\n", sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("Cancelled.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
