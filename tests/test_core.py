import json
import os

import pytest
from conftest import git

from tuicr_agent_runner.core import (
    RunnerError,
    check_current,
    github_coordinate,
    load_batch,
    locked,
    prepare,
    repository,
    snapshot,
    state_dir,
)


def batch(project, **kwargs):
    return prepare(
        project["repo"], project["slug"], project["tuicr"], "origin", kwargs.get("comment_ids", [])
    )


def save_session(p):
    p["session_path"].write_text(json.dumps(p["session"]))


def test_prepare_exact_cli_and_private_state(project):
    result = batch(project)
    assert result["comments"] == [
        {**project["comments"][0], "commit_id": None, "line_context": None}
    ]
    assert result["snapshot"]["head"] == project["head"]
    assert result["current_context"][0]["lines"] == ["1: print('hello')"]
    assert load_batch(project["repo"], result["id"]) == result
    assert git(project["repo"], "status", "--porcelain") == ""
    state = state_dir(project["repo"])
    assert state.stat().st_mode & 0o777 == 0o700
    assert (state / "batches" / f"{result['id']}.json").stat().st_mode & 0o777 == 0o600


@pytest.mark.parametrize("change", ["tracked", "untracked", "index", "head", "branch", "session"])
def test_drift_is_rejected(project, change):
    result = batch(project)
    repo = project["repo"]
    if change == "tracked":
        (repo / "hello.py").write_text("print('other')\n")
    elif change == "untracked":
        (repo / "new.txt").write_text("new")
    elif change == "index":
        git(repo, "update-index", "--chmod=+x", "hello.py")
    elif change == "head":
        git(repo, "commit", "--allow-empty", "-m", "new commit")
    elif change == "branch":
        git(repo, "checkout", "-b", "other")
    else:
        project["session"]["updated_at"] = "2026-01-02T00:00:00Z"
        save_session(project)
    with pytest.raises(RunnerError, match="changed"):
        check_current(repo, result)


def test_same_size_same_mtime_edit_is_detected(project):
    result = batch(project)
    file = project["repo"] / "hello.py"
    info = file.stat()
    file.write_text("print('world')\n")
    os.utime(file, ns=(info.st_atime_ns, info.st_mtime_ns))
    with pytest.raises(RunnerError):
        check_current(project["repo"], result)


def test_foreign_repo_revision_and_unsupported_sources(project):
    for field, value, error in [
        ("repo_path", str(project["tmp"]), "different worktree"),
        ("base_commit", "0" * 40, "revision"),
        ("diff_source", "pristine", "Unsupported"),
        ("version", "1.2", "schema"),
        ("branch_name", "other", "branch"),
    ]:
        old = project["session"][field]
        project["session"][field] = value
        save_session(project)
        with pytest.raises(RunnerError, match=error):
            batch(project)
        project["session"][field] = old
    save_session(project)


def test_ambiguous_session_and_explicit_path(project):
    rows = json.loads(project["listing_path"].read_text())
    rows.append({**rows[0], "path": str(project["tmp"] / "other.json")})
    project["listing_path"].write_text(json.dumps(rows))
    with pytest.raises(RunnerError, match="ambiguous"):
        batch(project)
    result = prepare(project["repo"], str(project["session_path"]), project["tuicr"], "origin", [])
    assert result["session"]["id"] == "session-1"


@pytest.mark.parametrize("value", [None, {}, [{"id": "missing"}], ["invalid"]])
def test_malformed_comments(project, value):
    project["comments_path"].write_text(json.dumps(value))
    with pytest.raises(RunnerError, match="schema"):
        batch(project)


def test_malformed_json(project):
    project["session_path"].write_text("{")
    with pytest.raises(RunnerError, match="Malformed"):
        batch(project)


def test_only_local_drafts_and_explicit_ids(project):
    other = {**project["comments"][0], "id": "submitted", "lifecycle_state": "submitted"}
    project["comments_path"].write_text(json.dumps([other, *project["comments"]]))
    assert len(batch(project)["comments"]) == 1
    with pytest.raises(RunnerError, match="local draft"):
        batch(project, comment_ids=["submitted"])
    project["comments_path"].write_text(json.dumps([other]))
    with pytest.raises(RunnerError, match="No local-draft"):
        batch(project)


def test_comment_path_escape(project):
    project["comments"][0]["path"] = "../secrets"
    project["comments_path"].write_text(json.dumps(project["comments"]))
    with pytest.raises(RunnerError, match="inside"):
        batch(project)


def test_batch_tampering(project):
    result = batch(project)
    path = state_dir(project["repo"]) / "batches" / f"{result['id']}.json"
    data = json.loads(path.read_text())
    data["comments"][0]["content"] = "changed"
    path.write_text(json.dumps(data))
    with pytest.raises(RunnerError, match="changed"):
        load_batch(project["repo"], result["id"])


def test_lock_is_nonblocking(project):
    with locked(project["repo"]):
        with pytest.raises(RunnerError, match="Another runner"), locked(project["repo"]):
            pass


def test_linked_worktree_has_separate_state(project):
    other = project["tmp"] / "linked"
    git(project["repo"], "worktree", "add", "-b", "linked", str(other))
    assert state_dir(other) != state_dir(project["repo"])
    with locked(project["repo"]), locked(other):
        pass
    assert repository(other) == other


def test_repo_root_required(project):
    directory = project["repo"] / "nested"
    directory.mkdir()
    with pytest.raises(RunnerError, match="worktree root"):
        repository(directory)


def test_git_env_overrides_are_removed(project, monkeypatch):
    monkeypatch.setenv("GIT_DIR", "/nonexistent/synthetic")
    assert snapshot(project["repo"])["head"] == project["head"]


def pr_session(project, host="github.com"):
    project["session"].update(
        diff_source="pull_request",
        repo_path="/__tuicr__/virtual",
        pr_session_key={
            "head_sha": project["head"],
            "number": 12,
            "repository": {"kind": "git_hub", "host": host, "owner": "test", "name": "repo"},
        },
    )
    save_session(project)


def test_pr_requires_exact_host_repo_head_and_clean_checkout(project):
    git(project["repo"], "remote", "add", "origin", "git@github.com:test/repo.git")
    pr_session(project)
    assert batch(project)["session"]["diff_source"] == "pull_request"
    for url in ["git@elsewhere.invalid:test/repo.git", "git@github.com:wrong/repo.git"]:
        git(project["repo"], "remote", "set-url", "origin", url)
        with pytest.raises(RunnerError, match="does not match"):
            batch(project)
    git(project["repo"], "remote", "set-url", "origin", "https://github.com/test/repo.git")
    (project["repo"] / "hello.py").write_text("dirty\n")
    with pytest.raises(RunnerError, match="clean checkout"):
        batch(project)
    git(project["repo"], "checkout", "--", "hello.py")
    project["session"]["pr_session_key"]["head_sha"] = "a" * 40
    save_session(project)
    with pytest.raises(RunnerError, match="exact reviewed"):
        batch(project)


@pytest.mark.parametrize("url", ["/local/repo", "https://github.com/repo", "file:///test/repo"])
def test_unverifiable_pr_remote(url):
    with pytest.raises(RunnerError):
        github_coordinate(url)


def test_commit_range_tip(project):
    project["session"].update(diff_source="commit_range", commit_range=[project["head"]])
    save_session(project)
    assert batch(project)["session"]["commit_range"] == [project["head"]]
    project["session"]["commit_range"] = ["a" * 40]
    save_session(project)
    with pytest.raises(RunnerError, match="range"):
        batch(project)


def test_selected_raw_anchors_only_in_prompt(project):
    from tuicr_agent_runner.core import prompt

    raw = project["session"]["files"]["hello.py"]["line_comments"]["1"][0]
    raw.update(commit_id=project["head"], line_context={"content": "old content", "old_line": 3})
    project["session"]["review_comments"].append(
        {
            "id": "not-selected",
            "content": "PRIVATE UNSELECTED FEEDBACK",
        }
    )
    save_session(project)
    result = batch(project)
    text = prompt(result)
    assert "old content" in text
    assert result["comments"][0]["commit_id"] == project["head"]
    assert "PRIVATE UNSELECTED FEEDBACK" not in text
    assert "review_comments" not in result["session"]


def test_symlinked_parent_escape_fails_closed(project):
    import shutil

    directory = project["repo"] / "directory"
    directory.mkdir()
    (directory / "file").write_text("tracked")
    git(project["repo"], "add", ".")
    outside = project["tmp"] / "outside"
    outside.mkdir()
    (outside / "file").write_text("must not read")
    shutil.rmtree(directory)
    directory.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RunnerError, match="escapes"):
        snapshot(project["repo"])


def test_session_changes_during_cli_capture(project):
    from conftest import executable

    script = executable(
        project["tmp"] / "changing-tuicr",
        f"""
import json, pathlib, sys
if sys.argv[2] == 'list':
    print(pathlib.Path({str(project["listing_path"])!r}).read_text())
else:
    path = pathlib.Path({str(project["session_path"])!r})
    value = json.loads(path.read_text())
    value['id'] = 'changed-session'
    path.write_text(json.dumps(value))
    print(pathlib.Path({str(project["comments_path"])!r}).read_text())
""",
    )
    with pytest.raises(RunnerError, match="changed while preparing"):
        prepare(project["repo"], project["slug"], script, "origin", [])


def test_submodule_rejected(project):
    git(
        project["repo"],
        "update-index",
        "--add",
        "--cacheinfo",
        f"160000,{project['head']},submodule",
    )
    with pytest.raises(RunnerError, match="Submodules"):
        snapshot(project["repo"])


def test_fork_origin_with_explicit_upstream(project):
    git(project["repo"], "remote", "add", "origin", "git@github.com:fork/repo.git")
    git(project["repo"], "remote", "add", "upstream", "git@github.com:test/repo.git")
    pr_session(project)
    with pytest.raises(RunnerError, match="does not match"):
        batch(project)
    result = prepare(project["repo"], project["slug"], project["tuicr"], "upstream", [])
    assert result["remote"] == "upstream"
    check_current(project["repo"], result)
