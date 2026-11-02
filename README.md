# tuicr-agent-runner

A small local helper: **review in tuicr → explicitly Send to Claude → a fresh
`claude -p` for that batch**. Start the watcher once; it serially delivers the same
Markdown that `y` copies. Your Claude workflow chooses the destination worktree.
The helper is persistent; Claude conversations are not. No TCP service, daemon
installation, scheduler, autostart, or clipboard monitoring.

## Install

Requires Python 3.11+, [uv](https://docs.astral.sh/uv/), a compatible tuicr build,
and an installed, authenticated Claude Code CLI. macOS and Linux only. The legacy
manual mode additionally requires Git.

```sh
git clone https://github.com/bitangent/tuicr-agent-runner.git
cd tuicr-agent-runner
uv tool install .
tuicr-agent-runner --help
```

## Start once, then send from tuicr

Use a tuicr build that includes **Send to Claude** (see compatibility below).
Start the foreground helper in a separate terminal, with the starting directory
where your usual Claude workflow/configuration lives:

```sh
tuicr-agent-runner watch --launch-dir /path/to/claude-starting-directory --trust-config
```

Keep reviewing in tuicr. When a batch is ready, press **`;` then `a`** (the default
`<leader>a` binding), or run **`:send-agent`**. `send_to_agent` is the configurable
binding. The helper starts one fresh Claude process, streams progress, records its
outcome, then waits for the next explicit batch. It uses the entire configured
`y` export, not only visible/filtered or newly added comments. Repeating send with
an unchanged export in the same tuicr process is deduplicated by tuicr. Use
`:send-agent!` only for an intentional resend after inspecting previous results; it creates a new batch ID.
A new ID is a new request, not content-level deduplication in the runner.

**Saving comments does not run Claude.** Neither `y` nor changing a review/session
file triggers execution. You can send while the helper is stopped; submissions
remain durable until it next starts. A queued batch is a snapshot, not a live view
of later edits. Sending does not clear comments, mark them addressed, or submit a
remote PR review.

`--launch-dir` is an actual process working directory and need not be a Git
repository. The helper does not check out a PR, validate a target checkout, add
routing text, or alter exported Markdown. If your Claude workflow needs commit/PR
identity, keep that information enabled in tuicr's export settings. Configure the
permissions and instructions your workflow needs in that starting directory/user
configuration; the watcher cannot infer the eventual destination.

### Queue and records

By default both programs use the same private per-user queue:

- Linux: `${XDG_STATE_HOME:-$HOME/.local/state}/tuicr/agent-queue` (an explicitly
  empty or relative `XDG_STATE_HOME` is rejected).
- macOS: `$HOME/Library/Application Support/tuicr/agent-queue`.

Set **`TUICR_AGENT_QUEUE_DIR` in both terminals** for a custom absolute path, or
pass `watch --queue-dir /absolute/path` and set the same path in tuicr's
`agent_queue_dir` configuration. These are local files, never a public service.
Read the [small versioned spool contract](docs/SPOOL.md) for limits and publication
semantics. The watcher polls the pending directory every second by default
(`--poll-interval`), not native filesystem events or saved comments. Polling never
starts Claude while idle. `--once` processes the current queue snapshot and exits.

```sh
tuicr-agent-runner jobs  # Add --queue-dir for an explicit custom queue.
```

Records and original submissions live in `QUEUE/jobs/BATCH_ID/`; invalid filenames
and duplicate submissions are preserved in `QUEUE/rejected/`; `jobs` lists both.
The original
Markdown and stdout/stderr may contain private source code. Keep the queue private
and local; do not publish it. New POSIX directories/files are private; existing
public or symlinked state is rejected, not silently chmodded. No automatic cleanup.

Only one watcher may use a queue, regardless of launch directory. Jobs run
serially because their eventual target is unknown. This is **not** a lock against
other queues, editors, Git, legacy manual runners, or unrelated agents.

Ctrl-C/SIGTERM cancels the current local process group **and stops the watcher**;
unclaimed queued batches remain for the next start. Per-batch `--timeout` defaults
to 900 seconds. Failed, denied, or timed-out batches are recorded and never retried;
the watcher continues with subsequent queued batches. Partial edits are not rolled
back. Process-group cancellation cannot stop remote work or detached descendants.

After SIGKILL/host crash, the watcher refuses all new jobs if any claim is
incomplete or still `claimed`/`running`, rather than overlap an orphaned Claude.
Inspect its logs and partial edits and stop any surviving process yourself. Then:

```sh
tuicr-agent-runner acknowledge-interrupted BATCH_ID --confirm-stopped
# Add --queue-dir if needed. Restart watch after acknowledging interrupted jobs.
```

This permanently marks the old ID `interrupted`; it does **not** retry it or prove
that the process has stopped. Review current code before explicitly sending a new
batch. Never delete attempt records as a shortcut to retry.

### Watch permissions and trust

`--trust-config` acknowledges that headless Claude loads user/project instructions,
configuration, plugins, hooks, and configured MCP servers without workspace trust
dialogs. Trust the queue writers and every directory your workflow will use.
The watcher passes `--permission-mode dontAsk`: permission checks still apply and
calls that would prompt are denied. It does not force a tools list, block your MCP
workflow, preapprove edits, or bypass permissions. Your existing rules/hooks apply;
repeat `--allow-tool 'Bash(your-command*)'` only for approvals you intend to grant.
Reported permission denials are terminal failures, never retried with broader access.

The exact export is the entire user prompt: the watcher adds **no** worktree,
commit/push/merge prohibition, or other instructions. The runner does not itself
invoke those actions; your Claude instructions and permissions govern what Claude
may do. This is not a sandbox. Normal Claude authentication/billing apply and real
batches send their Markdown to your configured provider. No real Claude request
has been used for development tests.

## Legacy manual prepare / show / run

These commands remain available for users who deliberately want checkout-locked
snapshots and approval hashes. Their validation and restricted tools policy are
unchanged; they are separate from the watcher and **not required** for Send to
Claude. The following sections describe this original manual mode.

### One feedback round

Start in the **exact worktree you want Claude to edit**. Save your tuicr comments,
then use another terminal (or exit tuicr):

```sh
cd /path/to/worktree
tuicr -w
# Add comments, then save/exit or use a second terminal.
tuicr review list --repo "$PWD"

# Copy the exact slug from the listing; never choose by timestamp.
tuicr-agent-runner prepare --repo "$PWD" --session 'owner/repo@branch/worktree'
# prepare prints the selected feedback, provenance, current line snippets,
# batch ID, approval hash, and the exact run command.

tuicr-agent-runner show --repo "$PWD" BATCH_ID
git diff
git diff --cached
# Inspect the selected comments AND the actual reviewed diff before approving.
tuicr-agent-runner run --repo "$PWD" BATCH_ID --approve APPROVAL_SHA256 --trust-repo
```

`prepare` selects all **local-draft** comments, regardless of author. Repeat
`--comment COMMENT_ID` to select only particular comments; submitted/pushed
comments are excluded. It does not infer who is human. Exact JSON paths from the
listing are accepted when a slug is ambiguous. It never selects the newest/active
session automatically. No comments are deleted or marked resolved.

To permit a particular test command, explicitly add an approval:

```sh
tuicr-agent-runner run --repo "$PWD" BATCH_ID --approve APPROVAL_SHA256 --trust-repo \
  --allow-tool 'Bash(uv run pytest*)' --timeout 900
```

Each batch can be attempted **once**. For any retry, inspect partial edits and
prepare/review a new batch. After completion, inspect `git diff`, check tests,
and refresh/reopen the intended review in tuicr. There is no automatic review
refresh integration.

### GitHub PR feedback

First check out the PR's exact head yourself in a clean worktree. The runner
never fetches or checks out a PR. Use its explicit `gh:owner/repo/pr/123` slug
(or listed JSON path). `prepare` verifies the stored PR head against `HEAD` and
the stored host/owner/repository against `origin`; use `--remote upstream` if
that is the remote naming the PR's target repository. For fork checkouts, discover
the target session with `tuicr review list --all`. The runner resolves your exact
selector across all sessions, then validates its checkout identity independently.
Other forges are not yet
supported. It does not check whether the remote PR has advanced since the saved
review; deliberately reviewing an older head remains possible.

### What manual mode checks

- Exact session ID/path and local worktree identity, or GitHub PR identity.
- Matching reviewed revision/branch; commit-range reviews must end at `HEAD`
  and use a clean checkout. File/pristine annotation sessions and submodules
  are unsupported.
- A SHA-256 fingerprint of `HEAD`, branch, index entries, tracked file bytes and
  nonignored untracked file bytes/modes. Symlink targets are hashed as links,
  not traversed. Ignored files, environment, Git/Claude configuration, and
  external symlink targets are **not** frozen.
- The entire saved tuicr session must be unchanged between prepare and run.
  Selected comment IDs, saved line context and commit-specific anchors are
  retained. Extra/unselected comments are not sent to Claude.

**This proves no detected drift from prepare to launch, not that historical
comments still match current code.** Tuicr can keep comments while rebasing its
session onto newer code. Human inspection is necessary, especially for old-side
and single-commit comments. Current line snippets are not historical context.
Do not edit the checkout or its review concurrently: the lock only coordinates
this runner, not editors, Git, tuicr, or other agents.

### Manual-mode permissions and trust

**Only use trusted repositories and trusted Claude configuration.** Running
`claude -p` loads user/project configuration, instructions, plugins and hooks;
workspace trust dialogs are omitted. Hooks and configured MCP server startup
can execute code even though this runner denies MCP tool calls. `--trust-repo`
acknowledges this, not a security sandbox.

The command uses `--permission-mode dontAsk`, built-in tools
`Read,Glob,Grep,Edit,Write,Bash`, and denies `mcp__*` tools. Read/edit tools are
preapproved; Bash requires your existing Claude permissions or `--allow-tool`.
An `--allowedTools` approval is **not** an exclusive whitelist; `--tools` limits
built-ins, but settings/hooks can still affect approvals. Denials are recorded
as a failed job, never retried with broader permissions. Broad Bash approvals
can run arbitrary code. Review test commands and inherited configuration.

The runner never invokes commit/push/merge and instructs Claude not to do so.
That instruction is not an enforceable boundary against a model with sufficiently
broad tools/permissions. Use OS isolation when you need one. No bypass-permissions
flag is supported. `--bare` is intentionally not the default: it skips CLAUDE.md
and does not use Claude subscription login. Normal CLI authentication/billing
apply; running a real batch sends context to your configured Claude provider.

See the official [headless guide](https://code.claude.com/docs/en/headless),
[CLI flags](https://code.claude.com/docs/en/cli-reference) and
[permissions](https://code.claude.com/docs/en/permissions).

### Manual-mode progress, cancellation and records

Assistant text and tool names stream to the terminal; complete stdout/stderr
and the final structured result are kept privately under:

```text
$(git rev-parse --absolute-git-dir)/tuicr-agent-runner/
  batches/     # immutable input snapshots and one-attempt markers
  jobs/        # job.json, stdout.jsonl, stderr.log per attempt
```

```sh
tuicr-agent-runner jobs --repo "$PWD"
```

These files can contain private code/feedback. They are outside the tracked tree,
with private directory/file permissions; do not publish them. Claude may also
retain its own transcripts. The final Claude session ID is recorded for
provenance, but this runner never resumes it. There is no automatic cleanup.

Ctrl-C/SIGTERM or `--timeout` terminates the local process group, escalating to
SIGKILL; it cannot cancel remote work or descendants that detach into a new
session. Partial edits are not rolled back. One runner holds the worktree lock
while active; after SIGKILL/host crash, confirm no orphaned Claude process remains
before starting another. A `running` record after such a crash is not evidence
of a live job. Repeated attempts are not automatically recovered.

Exit codes: `0` successful Claude result (not proof the requested fix is correct),
`1` Claude/setup/permission failure, `2` validation error, `124` timeout,
`130` cancellation. Missing/malformed final results fail closed. Output is capped
at 64 MiB per job, 8 MiB per JSON record. There is no output redaction.

## Compatibility and development

- Watch mode needs the companion **bitangent/tuicr Send to Claude** change
  (`send_to_agent` / `:send-agent`, spool v1). An older tuicr build does not publish
  submissions; watching saved session files is intentionally unsupported.
- Legacy manual integration targets **bitangent/tuicr 0.28.0, commit
  `8d4054d7995aa8c8a7363e24625c1da1f8d7a65a`**, session schema **1.3** and its
  [`review list/comments` contract](https://github.com/bitangent/tuicr/blob/8d4054d7995aa8c8a7363e24625c1da1f8d7a65a/docs/REVIEW_CLI.md).
  Version number alone does not guarantee those commands exist in another build.
- Claude flags/stream shape follow official docs checked **2026-11-02**.
  No real Claude version has been integration-tested yet: tests use fake
  executables only, never transmit a repository or consume model credits.
  A compatible CLI must support `-p --output-format stream-json --verbose`,
  `dontAsk`, and `--allowedTools`; legacy manual mode also uses `--tools` and
  `--disallowedTools`.
- No runtime dependencies. CI runs synthetic subprocess/Git tests on Python
  3.11–3.13 on Linux. macOS remains supported, but is not CI-verified.
  No Windows runner, daemon service, autostart, automatic Git operations, or
  publishing/release automation. Watch mode is VCS-agnostic: it delivers the full
  export (including any upstream threads) without fetching remote comments or
  reading VCS/session state itself. Legacy manual mode is Git-only.

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
