# Project guidance

Keep this a small explicit local CLI. The foreground watcher consumes only explicit
submissions; no daemon service, autostart, scheduler, or agent framework. It launches
fresh Claude from a configured directory without deciding or validating its target
worktree. Preserve the exact Markdown exported by tuicr.
Use only argv/stdin subprocess calls, never shell interpolation. Never bypass Claude
permissions. Never run a real Claude request in tests or publish private review data.

Run `uv sync --locked`, `uv run pytest`, `uv run ruff check .`, and
`uv run ruff format --check .`. Tests use synthetic tuicr sessions, fake executables,
and disposable Git repositories. Check upstream schemas before changing integrations.

Watch batches and job logs live in the private configured queue. Legacy manual
prepare/run records remain in the target worktree's Git directory. Do not
commit them, real feedback, credentials, or machine-specific paths. No automatic
commit/push/merge, review deletion, publication, or session resumption.
