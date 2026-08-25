"""smart_merge_file: a shorter transcript that is a prefix of a longer one is dropped.

Run: uv run pytest
"""

import json

from claude_migrate.cli import smart_merge_file, uuid_spine


def write(path, n, prefix="/Users/me", tag="u"):
    """A transcript of n lines whose embedded paths all use `prefix`."""
    path.write_text(
        "".join(
            json.dumps({"uuid": f"{tag}{i}", "cwd": f"{prefix}/app"}) + "\n"
            for i in range(n)
        )
    )


def lines(path):
    return sum(1 for _ in path.open())


def test_rewritten_superset_upgrades(tmp_path):
    """The case a byte-compare cannot see: strict superset sharing zero bytes.

    Import rewrites every embedded path, so the incoming file has no byte in common
    with its own earlier copy. Before the uuid-spine check this reported 'conflict'
    and left an .incoming behind on every single run.
    """
    src, dst = tmp_path / "s.jsonl", tmp_path / "d.jsonl"
    write(src, 50, "/home/svc")
    write(dst, 30, "/Users/me")
    assert smart_merge_file(src, dst) == "upgraded"
    assert lines(dst) == 50


def test_rewritten_subset_is_kept(tmp_path):
    """Incoming is the stale shorter side: keep the destination, write no .incoming."""
    src, dst = tmp_path / "s.jsonl", tmp_path / "d.jsonl"
    write(src, 30, "/home/svc")
    write(dst, 50, "/Users/me")
    assert smart_merge_file(src, dst) == "kept"
    assert lines(dst) == 50
    assert not (tmp_path / "d.jsonl.incoming").exists()


def test_equal_length_still_conflicts(tmp_path):
    """Equal spines are not a PROPER subset - neither side provably contains the other."""
    src, dst = tmp_path / "s.jsonl", tmp_path / "d.jsonl"
    write(src, 40, "/home/svc")
    write(dst, 40, "/Users/me")
    assert smart_merge_file(src, dst) == "conflict"


def test_diverged_still_conflicts(tmp_path):
    """Two sessions that forked must never be auto-resolved - that loses conversation."""
    src, dst = tmp_path / "s.jsonl", tmp_path / "d.jsonl"
    write(src, 60, tag="x")
    write(dst, 40, tag="u")
    assert smart_merge_file(src, dst) == "conflict"


def test_spine_ignores_lines_without_uuid(tmp_path):
    """Metadata lines (mode, bridge-session, file-history-snapshot) carry no uuid."""
    p = tmp_path / "t.jsonl"
    p.write_text(
        json.dumps({"type": "mode"}) + "\n"
        + json.dumps({"uuid": "a"}) + "\n"
        + "not json\n"
        + json.dumps({"uuid": "b"}) + "\n"
    )
    assert uuid_spine(p) == ["a", "b"]


def test_export_never_ships_incoming_files(tmp_path, monkeypatch):
    """The compounding engine: exporting a .incoming sends a conflict to the peer.

    It conflicts there, returns as .incoming.incoming, and gains a level per run until
    the name passes 255 bytes and every import for that project dies with Errno 36.
    """
    import tarfile

    from claude_migrate import cli

    project = tmp_path / "proj"
    project.mkdir()
    hist = tmp_path / "projects" / cli.encode_path(str(project))
    hist.mkdir(parents=True)
    write(hist / "a.jsonl", 3)
    write(hist / "a.jsonl.incoming", 3)
    write(hist / "a.jsonl.incoming.incoming", 3)

    monkeypatch.setattr(cli, "PROJECTS_DIR", tmp_path / "projects")
    monkeypatch.setattr(cli, "CLAUDE_DIR", tmp_path / "claude")
    (tmp_path / "claude" / "sessions").mkdir(parents=True)

    assert cli.export_history(str(project), str(tmp_path)) == 0
    archive = next(tmp_path.glob("*.tar.gz"))
    with tarfile.open(archive) as tf:
        names = tf.getnames()
    assert any(n.endswith("project-history/a.jsonl") for n in names)
    assert not [n for n in names if ".incoming" in n], names


def test_merge_trees_skips_incoming(tmp_path):
    """Second line of defence, for archives made before the export filter existed."""
    from claude_migrate import cli

    src, dst = tmp_path / "s", tmp_path / "d"
    (src / "sub").mkdir(parents=True)
    dst.mkdir()
    write(src / "a.jsonl", 3)
    write(src / "a.jsonl.incoming", 3)
    write(src / "sub" / "b.jsonl.incoming.incoming", 3)

    cli.merge_trees(src, dst)
    assert (dst / "a.jsonl").exists()
    assert not list(dst.rglob("*.incoming*"))


def test_no_spine_falls_through_to_conflict(tmp_path):
    """Two uuid-less files must not compare as trivially equal empty spines."""
    src, dst = tmp_path / "s.jsonl", tmp_path / "d.jsonl"
    src.write_text(json.dumps({"type": "mode", "a": 1}) + "\n")
    dst.write_text(json.dumps({"type": "mode", "a": 2}) + "\n" + json.dumps({"b": 3}) + "\n")
    assert smart_merge_file(src, dst) == "conflict"
