"""Regression tests for the features merged in from the long-running local branch.

`smart_merge_test.py` covers merging; these cover the other half of the CLI —
content rewriting, whole-setup export/import, user-message extraction, and install.
"""
import json

from claude_migrate import cli


def session_line(uuid, role, text, cwd, **extra):
    return json.dumps({
        "uuid": uuid,
        "type": role,
        "cwd": cwd,
        "message": {"role": role, "content": text},
        **extra,
    })


def make_project(tmp_path, name="alpha"):
    """A project directory plus one transcript that embeds its own absolute path."""
    project = tmp_path / "work" / name
    project.mkdir(parents=True)
    hist = tmp_path / "projects" / cli.encode_path(str(project))
    hist.mkdir(parents=True)
    (hist / "s1.jsonl").write_text("\n".join([
        session_line("u1", "user", f"look at {project}/file.py", str(project)),
        session_line("u2", "assistant", "reading ./file.py", str(project)),
        session_line("u3", "user", "second prompt", str(project)),
    ]) + "\n")
    return project, hist


def patch_dirs(monkeypatch, tmp_path):
    monkeypatch.setattr(cli, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(cli, "CLAUDE_DIR", tmp_path / "claude")
    (tmp_path / "claude" / "sessions").mkdir(parents=True, exist_ok=True)


def test_replace_references_rewrites_the_embedded_absolute_path(tmp_path, monkeypatch):
    """The point of the flag: the old cwd must not survive inside the transcript."""
    patch_dirs(monkeypatch, tmp_path)
    project, _ = make_project(tmp_path)
    new = tmp_path / "work" / "beta"
    new.mkdir()

    assert cli.migrate(str(project), str(new), replace_references=True) == 0

    moved = (tmp_path / "projects" / cli.encode_path(str(new)) / "s1.jsonl").read_text()
    assert f"{new}/file.py" in moved
    assert str(project) not in moved


def test_dry_run_writes_nothing(tmp_path, monkeypatch):
    patch_dirs(monkeypatch, tmp_path)
    project, _ = make_project(tmp_path)
    new = tmp_path / "work" / "beta"
    new.mkdir()

    assert cli.migrate(str(project), str(new), dry_run=True, replace_references=True) == 0
    assert not (tmp_path / "projects" / cli.encode_path(str(new))).exists()


def test_export_all_then_import_all_round_trips(tmp_path, monkeypatch):
    patch_dirs(monkeypatch, tmp_path)
    project, hist = make_project(tmp_path)
    original = (hist / "s1.jsonl").read_text()

    out = tmp_path / "archives"
    out.mkdir()
    assert cli.export_all_history(str(out)) == 0
    archive = next(out.glob("*.tar.gz"))

    # Wipe local history, then restore it from the archive.
    (hist / "s1.jsonl").unlink()
    hist.rmdir()
    assert cli.import_all_history(str(archive)) == 0
    assert (hist / "s1.jsonl").read_text() == original


def test_user_messages_returns_only_human_turns(tmp_path):
    """Assistant turns, sidechains and meta entries are not things the user typed."""
    f = tmp_path / "s.jsonl"
    f.write_text("\n".join([
        session_line("u1", "user", "real prompt", "/x"),
        session_line("u2", "assistant", "assistant reply", "/x"),
        session_line("u3", "user", "subagent prompt", "/x", isSidechain=True),
        session_line("u4", "user", "meta entry", "/x", isMeta=True),
        session_line("u5", "user", "another real prompt", "/x"),
    ]) + "\n")

    assert cli.extract_user_messages(f) == ["real prompt", "another real prompt"]


def test_install_writes_the_skill_and_not_a_command(tmp_path, monkeypatch):
    """install() must produce a skill; a bare slash command is the thing it replaced."""
    monkeypatch.setattr(cli, "SKILLS_DIR", tmp_path / "skills")
    monkeypatch.setattr(cli, "COMMANDS_DIR", tmp_path / "commands")

    assert cli.install() == 0

    skill = tmp_path / "skills" / "migrate" / "SKILL.md"
    assert skill.is_file()
    assert (tmp_path / "skills" / "migrate" / "references" / "platform-notes.md").is_file()
    assert not (tmp_path / "commands" / "migrate.md").exists()
    # the flags carried over from the retired migrate.md must still be documented
    assert "--replace-userpath" in skill.read_text()
