# tuicr-agent-runner

A small local CLI: **review in tuicr → prepare a feedback batch → inspect → run a
fresh `claude -p`**. No persistent conversation, daemon, polling loop, TCP service,
MCP server, scheduler, or changes to tuicr.

## Install

Requires Python 3.11+, Git, [uv](https://docs.astral.sh/uv/), tuicr and an installed,
authenticated Claude Code CLI. macOS and Linux only.

```sh
git clone https://github.com/bitangent/tuicr-agent-runner.git
cd tuicr-agent-runner
uv tool install .
tuicr-agent-runner --help
```

## One feedback round

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
and refresh/reopen the intended review in tuicr. There is no automatic refresh
integration and no `tuicr watch` command supplied here.

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

## What gets checked

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

## Permissions and trust

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

## Progress, cancellation and records

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

- Tuicr integration targets **bitangent/tuicr 0.28.0, commit
  `8d4054d7995aa8c8a7363e24625c1da1f8d7a65a`**, session schema **1.3** and its
  [`review list/comments` contract](https://github.com/bitangent/tuicr/blob/8d4054d7995aa8c8a7363e24625c1da1f8d7a65a/docs/REVIEW_CLI.md).
  Version number alone does not guarantee those commands exist in another build.
- Claude flags/stream shape follow official docs checked **2026-11-02**.
  No real Claude version has been integration-tested yet: tests use fake
  executables only, never transmit a repository or consume model credits.
  A compatible CLI must support `-p --output-format stream-json --verbose`,
  `dontAsk`, `--tools`, `--disallowedTools`, and `--allowedTools`.
- No runtime dependencies. CI runs synthetic subprocess/Git tests on Python
  3.11–3.13, Linux and macOS. No Windows, jj/hg, daemon, auto-commit, push, merge,
  remote comment ingestion, publishing, or release automation.

```sh
uv sync --locked
uv run pytest
uv run ruff check .
uv run ruff format --check .
```
