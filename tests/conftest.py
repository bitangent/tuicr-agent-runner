from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest


def git(repo: Path, *args: str) -> str:
    return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()


def executable(path: Path, body: str) -> str:
    path.write_text(f"#!{sys.executable}\n" + body)
    path.chmod(0o700)
    return str(path)


@pytest.fixture
def project(tmp_path):
    repo = tmp_path / "repo with spaces"
    repo.mkdir()
    git(repo, "init", "-b", "main")
    git(repo, "config", "user.email", "test@example.invalid")
    git(repo, "config", "user.name", "Synthetic Test")
    (repo / "hello.py").write_text("print('hello')\n")
    git(repo, "add", ".")
    git(repo, "commit", "-m", "synthetic fixture")
    head = git(repo, "rev-parse", "HEAD")
    comment = {
        "id": "comment-1",
        "location": "hello.py:1",
        "path": "hello.py",
        "start_line": 1,
        "end_line": 1,
        "side": "new",
        "comment_type": "issue",
        "author": "reviewer",
        "lifecycle_state": "local_draft",
        "created_at": "2026-01-01T00:00:00Z",
        "content": "Handle empty input.",
    }
    raw_comment = {
        k: comment[k]
        for k in (
            "id",
            "content",
            "comment_type",
            "author",
            "lifecycle_state",
            "created_at",
            "side",
        )
    }
    raw_comment.update(line_context=None, line_range=None, commit_id=None)
    session = {
        "id": "session-1",
        "version": "1.3",
        "repo_path": str(repo),
        "branch_name": "main",
        "base_commit": head,
        "diff_source": "working_tree",
        "commit_range": None,
        "pr_session_key": None,
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2026-01-01T00:00:00Z",
        "review_comments": [],
        "session_notes": None,
        "files": {
            "hello.py": {
                "path": "hello.py",
                "reviewed": False,
                "status": "modified",
                "file_comments": [],
                "line_comments": {"1": [raw_comment]},
                "reviewed_hunks": [],
                "content_hash": 123,
            }
        },
    }
    session_path = tmp_path / "session.json"
    session_path.write_text(json.dumps(session))
    comments_path = tmp_path / "comments.json"
    comments_path.write_text(json.dumps([comment]))
    listing_path = tmp_path / "listing.json"
    listing_path.write_text(
        json.dumps(
            [
                {
                    "slug": "test/repo@main/worktree",
                    "path": str(session_path),
                    "kind": "local",
                    "updated_at": session["updated_at"],
                    "comment_count": 1,
                    "reviewed_count": 0,
                    "file_count": 1,
                    "anchor": "main",
                    "active": True,
                }
            ]
        )
    )
    tuicr = executable(
        tmp_path / "fake-tuicr",
        f"""
import pathlib, sys
assert sys.argv[1] == 'review'
if sys.argv[2] == 'list':
    assert sys.argv[3:] == ['--all']
    print(pathlib.Path({str(listing_path)!r}).read_text())
elif sys.argv[2] == 'comments':
    assert sys.argv[3:] == ['--session', {str(session_path)!r}, '--repo', {str(repo)!r}]
    print(pathlib.Path({str(comments_path)!r}).read_text())
else:
    raise AssertionError('runner must never modify tuicr')
""",
    )
    return {
        "repo": repo,
        "head": head,
        "session": session,
        "session_path": session_path,
        "comments": [comment],
        "comments_path": comments_path,
        "listing_path": listing_path,
        "tuicr": tuicr,
        "tmp": tmp_path,
        "slug": "test/repo@main/worktree",
    }


@pytest.fixture
def claude_factory(tmp_path):
    def make(body=None):
        if body is None:
            body = """
import json, pathlib, sys
prompt = sys.stdin.read()
pathlib.Path('received.txt').write_text(prompt)
pathlib.Path('argv.json').write_text(json.dumps(sys.argv[1:]))
print(json.dumps({'type': 'assistant', 'message': {'content': [
    {'type': 'tool_use', 'name': 'Edit'}, {'type': 'text', 'text': 'Working.'}]}}))
print(json.dumps({'type': 'result', 'subtype': 'success', 'is_error': False,
                  'session_id': 'fake-session-123', 'result': 'Addressed comment-1.',
                  'permission_denials': []}))
"""
        return executable(tmp_path / f"fake-claude-{os.urandom(4).hex()}", body)

    return make
