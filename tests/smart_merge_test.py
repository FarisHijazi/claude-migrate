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


def test_no_spine_falls_through_to_conflict(tmp_path):
    """Two uuid-less files must not compare as trivially equal empty spines."""
    src, dst = tmp_path / "s.jsonl", tmp_path / "d.jsonl"
    src.write_text(json.dumps({"type": "mode", "a": 1}) + "\n")
    dst.write_text(json.dumps({"type": "mode", "a": 2}) + "\n" + json.dumps({"b": 3}) + "\n")
    assert smart_merge_file(src, dst) == "conflict"
