# Tuicr → agent runner spool, version 1

This local, private directory is a delivery interface, not a review-session format.
Only an explicit **Send to Claude** action publishes a batch. Saving comments,
changing files, or copying with `y` does not publish one.

## Location

Both programs honor `TUICR_AGENT_QUEUE_DIR`. The runner's `--queue-dir` takes
precedence. Tuicr also supports `agent_queue_dir` in its configuration; when using
that setting, give the runner the same path explicitly or through the environment.
Paths must be nonempty and absolute; `~` is not expanded in queue settings.
Defaults:

- Linux: `$XDG_STATE_HOME/tuicr/agent-queue`, or
  `$HOME/.local/state/tuicr/agent-queue` when `XDG_STATE_HOME` is unset.
- macOS: `$HOME/Library/Application Support/tuicr/agent-queue`.
- Windows producer: `%LOCALAPPDATA%/tuicr/agent-queue`. The runner requires POSIX
  process groups and locking and does **not** run on Windows.

An explicitly set empty or relative `XDG_STATE_HOME` is an error, not a fallback.
On POSIX, the queue root and managed subdirectories must be owned by the current
user with no group/other access (normally `0700`); final symlink directories are
rejected. Files must be regular, current-user-owned and private (normally `0600`).
Normal home/state ancestors need not be private. This is not a defense against
other software running as the same user; only trusted writers may use the queue.
Use a local filesystem supporting atomic publication, directory fsync, and flock,
not a shared network/synced directory.

## Producer publication

Generate a fresh UUID represented as **32 lowercase hexadecimal characters**.
Serialize this UTF-8 JSON envelope:

```json
{"version":1,"id":"00000000000040008000000000000001","markdown":"# Review\n…\n"}
```

`markdown` is exactly the string the existing `y` exporter would copy, with the
same export settings. Do not trim, normalize newlines, wrap, append instructions,
or infer/insert worktree metadata. Maximum Markdown size is **8 MiB encoded as
UTF-8**, maximum envelope size **16 MiB UTF-8**. The version is integer `1` and the
ID must exactly match the filename.

Create a private temporary file within `pending/`, write the complete envelope,
flush and fsync, then publish atomically and without overwriting an existing ID as
`pending/<id>.json`. Rename with no-replace semantics or hard-link-plus-unlink
publication is valid. Sync the pending directory before confirming submission.
Temporary names must not end in `.json`. Published files are immutable. Only
complete published `.json` entries are consumed; the clipboard is never watched.

## Consumer ownership and delivery semantics

The foreground watcher holds one queue-wide `watch.lock` using `flock`. It polls
only `pending/` (one second by default), selects oldest file modification time
then filename, and executes one fresh `claude -p` at a time. There is no socket,
service installation, native filesystem event dependency, scheduler, or autostart.
One queue must be shared by all reviews intended to serialize; different queues
and other editors/agents/manual runners are not coordinated.

The watcher creates and fsyncs `jobs/<id>/` as a permanent claim **before launch**,
then writes a durable `job.json`, moves the submission into that directory, and
passes its decoded Markdown unchanged to Claude stdin. Records progress through
`claimed` → `running` → a terminal outcome. Terminal outcomes are `succeeded`,
`failed`, `permission_denied`, `timed_out`, `cancelled`, `rejected`, or `interrupted`.
`stdout.jsonl` and `stderr.log` retain the local process output, including the full
final event; `job.json` keeps only bounded outcome metadata. Records retain the
launch directory, not a claimed destination worktree. No batch ID is retried.

Malformed submissions receive a rejected record. Invalid filenames and duplicate
IDs are moved under `rejected/<unique-id>/`, preserving their bytes and reason
without replacing the original attempt. A malformed record, incomplete claim, or
`claimed`/`running` record on restart blocks **all** new execution: a child process
might have survived the watcher or already edited files. The operator must stop
any orphan, inspect edits/logs, then explicitly acknowledge that batch as stopped.
Acknowledgment retains the permanent claim and never resubmits it. Failed attempts
also remain consumed; review the current state before explicitly sending a new ID.

Do not delete claim directories to retry work. There is no automatic cleanup,
rollback, comment deletion, Git commit/push/merge, or session resumption. Queued
feedback and logs may contain private source code; do not publish them.
