#!/usr/bin/env python3
"""Manage Claude Code conversation history when moving/copying project directories.

When you move/copy a project directory, Claude Code loses track of its
conversation history because it's keyed by encoded absolute path.
This tool copies/moves ~/.claude/projects/<old-encoded>/ to
~/.claude/projects/<new-encoded>/ and appends a migration notice to
the most recent session so Claude knows paths have changed.
"""

import argparse
import filecmp
import getpass
import importlib.resources
import json
import os
import platform
import re
import shutil
import sys
import tarfile
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

CLAUDE_DIR = Path(os.environ.get("CLAUDE_CONFIG_DIR", Path.home() / ".claude"))
PROJECTS_DIR = CLAUDE_DIR / "projects"
COMMANDS_DIR = CLAUDE_DIR / "commands"
SKILLS_DIR = CLAUDE_DIR / "skills"
# Read lazily: a module-level read makes an unrelated subcommand fail at import time
# if the packaged resource is ever missing.
SKILL_FILES = ("SKILL.md", "references/platform-notes.md")
# ---------------------------------------------------------------------------
# Path encoding & user detection
# ---------------------------------------------------------------------------


def encode_path(p: str) -> str:
    """Encode a directory path the way Claude Code does: replace / and . with -."""
    return str(Path(p).resolve()).replace("/", "-").replace(".", "-")


USER_PATH_PREFIXES = ("/Users/", "/home/")


def _detect_user(p: str) -> str | None:
    for prefix in USER_PATH_PREFIXES:
        if p.startswith(prefix):
            rest = p[len(prefix):]
            user = rest.split("/", 1)[0]
            return user or None
    return None


def detect_userpath_pair(old_path: str, new_path: str) -> tuple[str, str] | None:
    """Auto-derive a (from_user, to_user) pair from the migration endpoints."""
    from_user = _detect_user(old_path)
    to_user = _detect_user(new_path)
    if not to_user:
        try:
            to_user = getpass.getuser()
        except Exception:
            to_user = None
    if not from_user or not to_user or from_user == to_user:
        return None
    return (from_user, to_user)


def parse_userpath_flag(value: str | None, old_path: str, new_path: str) -> tuple[str, str] | None:
    """Resolve the --replace-userpath CLI value into a (from, to) pair or None."""
    if value is None:
        return None
    if value == "auto":
        return detect_userpath_pair(old_path, new_path)
    if ":" not in value:
        raise ValueError(
            f"--replace-userpath value must be FROM:TO, got: {value!r}. "
            f"Use --replace-userpath=FROM:TO (with =) or pass it last."
        )
    from_user, _, to_user = value.partition(":")
    if not from_user or not to_user:
        raise ValueError(f"--replace-userpath value must be FROM:TO, got: {value!r}")
    if from_user == to_user:
        return None
    return (from_user, to_user)


# ---------------------------------------------------------------------------
# Content rewriting: --replace-userpath and --replace-references
# ---------------------------------------------------------------------------

# Boundary chars that indicate "end of a path token" — used as a positive
# lookahead after a path to avoid eating into adjacent characters.
_PATH_BOUNDARY = r'(?=/|["\s\\\'`)\]:,;<>]|$)'

# Relative path token: starts with ./ or ../, followed by safe chars.
# - Negative lookbehind prevents matching mid-token (e.g. in URLs like foo/./bar).
# - Negative lookbehind at end keeps trailing dot/slash out of the match.
_REL_PATH_PATTERN = re.compile(r'(?<![\w./])\.{1,2}/[\w./\-]+(?<![./])')


def apply_userpath_to_string(s: str, from_user: str, to_user: str) -> str:
    """Rewrite /Users/<from>/, /home/<from>/ and their encoded forms."""
    out = s
    # Path form, followed by another segment.
    for prefix in ("/Users/", "/home/"):
        out = out.replace(f"{prefix}{from_user}/", f"{prefix}{to_user}/")
        # Path form at a boundary (end-of-string, quote, etc.)
        out = re.sub(
            re.escape(f"{prefix}{from_user}") + r'(?=["\s\\\'`)\]:,;<>]|$)',
            f"{prefix}{to_user}",
            out,
        )
    # Encoded forms used in ~/.claude/projects/<encoded> dir names.
    for prefix in ("-Users-", "-home-"):
        out = out.replace(f"{prefix}{from_user}-", f"{prefix}{to_user}-")
    return out


def apply_references_to_string(s: str, old_cwd: str, new_cwd: str) -> str:
    """Rewrite absolute paths under old_cwd; rewrite relative paths escaping old_cwd."""
    if old_cwd == new_cwd:
        return s

    # 1. Absolute paths under old_cwd → swap prefix. Lookahead bounds the match
    # so /old/cwd does not consume /old/cwdbar.
    s = re.sub(re.escape(old_cwd) + _PATH_BOUNDARY, new_cwd, s)

    # 2. Encoded form (e.g. -Users-alice-projects-foo).
    old_enc = old_cwd.replace("/", "-").replace(".", "-")
    new_enc = new_cwd.replace("/", "-").replace(".", "-")
    if old_enc != new_enc and old_enc:
        s = re.sub(re.escape(old_enc) + r'(?=[-_/"\s\\\'`)\]:,;<>]|$)', new_enc, s)

    # 3. Relative paths: resolve against old_cwd. If inside, keep relative
    # (still works after move). If escapes, replace with the original absolute
    # path (the external thing didn't move).
    def _rewrite_rel(m: re.Match) -> str:
        rel = m.group(0)
        try:
            abs_resolved = os.path.normpath(os.path.join(old_cwd, rel))
        except Exception:
            return rel
        try:
            Path(abs_resolved).relative_to(old_cwd)
            return rel  # inside old_cwd — relative path still valid at new_cwd
        except ValueError:
            return abs_resolved  # escapes — pin to its absolute original

    return _REL_PATH_PATTERN.sub(_rewrite_rel, s)


def transform_string(
    s: str,
    old_cwd: str,
    new_cwd: str,
    userpath_map: tuple[str, str] | None,
    replace_refs: bool,
) -> str:
    if userpath_map:
        s = apply_userpath_to_string(s, *userpath_map)
    if replace_refs:
        s = apply_references_to_string(s, old_cwd, new_cwd)
    return s


def _transform_obj(obj, old_cwd, new_cwd, userpath_map, replace_refs, changes):
    if isinstance(obj, str):
        new = transform_string(obj, old_cwd, new_cwd, userpath_map, replace_refs)
        if new != obj:
            changes.append((obj, new))
        return new
    if isinstance(obj, dict):
        return {
            k: _transform_obj(v, old_cwd, new_cwd, userpath_map, replace_refs, changes)
            for k, v in obj.items()
        }
    if isinstance(obj, list):
        return [
            _transform_obj(x, old_cwd, new_cwd, userpath_map, replace_refs, changes)
            for x in obj
        ]
    return obj


def rewrite_history_content(
    history_dir: Path,
    old_cwd: str,
    new_cwd: str,
    *,
    userpath_map: tuple[str, str] | None,
    replace_refs: bool,
    dry_run: bool,
) -> int:
    """Walk JSON/JSONL files in history_dir and rewrite path strings.

    Returns total number of replacements made (counted across all string occurrences).
    Prints each unique before -> after pair.
    """
    if not userpath_map and not replace_refs:
        return 0

    all_changes: list[tuple[Path, str, str]] = []

    for f in sorted(history_dir.rglob("*")):
        if not f.is_file():
            continue
        if f.suffix == ".jsonl":
            file_changed = False
            new_lines: list[str] = []
            for line in f.read_text().split("\n"):
                stripped = line.strip()
                if not stripped:
                    new_lines.append(line)
                    continue
                try:
                    obj = json.loads(stripped)
                except json.JSONDecodeError:
                    new_lines.append(line)
                    continue
                line_changes: list[tuple[str, str]] = []
                new_obj = _transform_obj(obj, old_cwd, new_cwd, userpath_map, replace_refs, line_changes)
                if line_changes:
                    file_changed = True
                    all_changes.extend((f, b, a) for b, a in line_changes)
                    new_lines.append(json.dumps(new_obj))
                else:
                    new_lines.append(line)
            if file_changed and not dry_run:
                f.write_text("\n".join(new_lines))
        elif f.suffix == ".json":
            try:
                obj = json.loads(f.read_text())
            except json.JSONDecodeError:
                continue
            file_changes: list[tuple[str, str]] = []
            new_obj = _transform_obj(obj, old_cwd, new_cwd, userpath_map, replace_refs, file_changes)
            if file_changes:
                all_changes.extend((f, b, a) for b, a in file_changes)
                if not dry_run:
                    f.write_text(json.dumps(new_obj, indent=2))

    if all_changes:
        unique = sorted({(b, a) for _, b, a in all_changes})
        files = {f for f, _, _ in all_changes}
        verb = "Would replace" if dry_run else "Replaced"
        print(f"\n{verb} {len(all_changes)} occurrence(s) of {len(unique)} unique path(s) across {len(files)} file(s):")
        max_show = 80
        for i, (before, after) in enumerate(unique):
            if i >= max_show:
                print(f"  ... ({len(unique) - max_show} more)")
                break
            b = before if len(before) <= 140 else before[:137] + "..."
            a = after if len(after) <= 140 else after[:137] + "..."
            print(f"  {b}  ->  {a}")
    return len(all_changes)


# ---------------------------------------------------------------------------
# Smart merge (existing logic)
# ---------------------------------------------------------------------------


def find_latest_session(history_dir: Path) -> Path | None:
    sessions = sorted(history_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    return sessions[0] if sessions else None


def append_migration_notice(session_file: Path, old_path: str, new_path: str, dry_run: bool) -> bool:
    lines = session_file.read_text().strip().split("\n")
    session_id = None
    last_uuid = None
    for line in reversed(lines):
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if "sessionId" in obj:
            session_id = obj["sessionId"]
        if "uuid" in obj and last_uuid is None:
            last_uuid = obj["uuid"]
        if session_id and last_uuid:
            break

    if not session_id:
        print(f"  Could not find sessionId in {session_file.name}, skipping notice")
        return False

    notice = (
        f"NOTE: This conversation's project directory has been moved.\n"
        f"Old path: {old_path}\n"
        f"New path: {new_path}\n"
        f"All file paths from the old location now exist at the new location. "
        f"When referencing files from earlier in this conversation, use the new path prefix."
    )
    msg = {
        "parentUuid": last_uuid or str(uuid.uuid4()),
        "isSidechain": False,
        "userType": "external",
        "cwd": new_path,
        "sessionId": session_id,
        "type": "user",
        "message": {"role": "user", "content": [{"type": "text", "text": notice}]},
        "uuid": str(uuid.uuid4()),
        "timestamp": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z",
    }
    if dry_run:
        print(f"  Would append migration notice to {session_file.name}")
    else:
        existing = session_file.read_bytes()
        sep = b"" if existing.endswith(b"\n") else b"\n"
        with open(session_file, "ab") as f:
            f.write(sep + json.dumps(msg).encode() + b"\n")
        print(f"  Appended migration notice to {session_file.name}")
    return True


def merge_trees(src: Path, dst: Path, *, dry_run: bool = False) -> dict[str, int]:
    dst.mkdir(parents=True, exist_ok=True)
    stats: dict[str, int] = {"added": 0, "identical": 0, "upgraded": 0, "kept": 0, "conflict": 0}
    for src_file in src.rglob("*"):
        if not src_file.is_file():
            continue
        # Belt and braces with the export-side filter: a .incoming is a local merge
        # artifact. Merging one can only ever create .incoming.incoming.
        if ".incoming" in src_file.name:
            continue
        rel = src_file.relative_to(src)
        dst_file = dst / rel
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        if dry_run:
            print(f"  [DRY RUN] would merge: {rel}")
        else:
            status = smart_merge_file(src_file, dst_file)
            stats[status] += 1
            if status not in ("identical", "kept"):
                print(f"  {status}: {rel}")
    return stats


def print_merge_stats(stats: dict[str, int]) -> None:
    print("\nMerge result:")
    for k, v in stats.items():
        if v:
            print(f"  {k}: {v}")
    if stats.get("conflict"):
        print(f"\n⚠ {stats['conflict']} conflict(s) saved as .incoming files")


# ---------------------------------------------------------------------------
# Folder ops
# ---------------------------------------------------------------------------


def copy_folder(old_resolved: str, new_resolved: str, *, dry_run: bool) -> int:
    old_dir = Path(old_resolved)
    new_dir = Path(new_resolved)
    prefix = "[DRY RUN] " if dry_run else ""

    print(f"{prefix}Copying project folder:")
    print(f"  {old_dir} -> {new_dir}")

    if not old_dir.is_dir():
        print(f"  Source directory not found: {old_dir}")
        return 1
    if new_dir.exists():
        print(f"  ERROR: destination already exists: {new_dir}")
        print(f"  Either delete the target folder first, or remove the --folder flag.")
        return 1

    if dry_run:
        n = sum(1 for _ in old_dir.rglob("*"))
        print(f"  Would copy ~{n} entries to {new_dir}")
        return 0

    new_dir.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(old_dir, new_dir)
    print(f"  Copied folder to {new_dir}")
    return 0


def delete_folder(path: str, *, dry_run: bool) -> int:
    p = Path(path)
    prefix = "[DRY RUN] " if dry_run else ""
    print(f"{prefix}Deleting source folder: {p}")
    if not p.is_dir():
        print(f"  Folder not found: {p}")
        return 1
    if dry_run:
        n = sum(1 for _ in p.rglob("*"))
        print(f"  Would delete ~{n} entries")
        return 0
    shutil.rmtree(p)
    print(f"  Deleted {p}")
    return 0


# ---------------------------------------------------------------------------
# Core migrate
# ---------------------------------------------------------------------------


def migrate(
    old_path: str,
    new_path: str,
    *,
    dry_run: bool = False,
    merge: bool = False,
    folder: bool = False,
    delete_history: bool = False,
    delete_dir: bool = False,
    userpath_value: str | None = None,
    replace_references: bool = False,
) -> int:
    if delete_dir and not folder:
        print("ERROR: --delete-dir requires --folder (nothing else copies the directory).")
        return 2

    old_resolved = str(Path(old_path).resolve())
    new_resolved = str(Path(new_path).resolve())

    try:
        userpath_map = parse_userpath_flag(userpath_value, old_resolved, new_resolved)
    except ValueError as exc:
        print(f"ERROR: {exc}")
        return 2
    if userpath_value and userpath_map is None:
        print(f"  --replace-userpath: no rewrite needed (users match or undetectable)")

    if folder:
        rc = copy_folder(old_resolved, new_resolved, dry_run=dry_run)
        if rc != 0:
            return rc
        print()

    old_history = PROJECTS_DIR / encode_path(old_path)
    new_history = PROJECTS_DIR / encode_path(new_path)
    prefix = "[DRY RUN] " if dry_run else ""

    action = "Moving" if delete_history else "Copying"
    print(f"{prefix}{action} Claude Code history:")
    print(f"  {old_resolved} -> {new_resolved}")
    print(f"  {old_history}")
    print(f"  {new_history}")
    print()

    if not old_history.is_dir():
        print(f"  No history found at {old_history}")
        return 1

    if new_history.exists() and not merge:
        print(f"  WARNING: {new_history} already exists, skipping to avoid data loss.")
        print(f"  Use --merge to smart-merge into existing history.")
        return 1

    if new_history.exists() and merge:
        print(f"  Destination exists, merging...")
        stats = merge_trees(old_history, new_history, dry_run=dry_run)
        if not dry_run:
            print_merge_stats(stats)
    else:
        n_files = sum(1 for f in old_history.rglob("*") if f.is_file())
        if dry_run:
            print(f"  Would copy {n_files} files")
        else:
            shutil.copytree(old_history, new_history)
            print(f"  Copied {n_files} files")

    # Content rewriting on the destination (in dry-run, scan source for preview).
    rewrite_target = new_history if (not dry_run and new_history.exists()) else old_history
    if userpath_map or replace_references:
        print()
        rewrite_history_content(
            rewrite_target,
            old_resolved,
            new_resolved,
            userpath_map=userpath_map,
            replace_refs=replace_references,
            dry_run=dry_run,
        )

    # Migration notice on the latest session.
    latest = find_latest_session(rewrite_target)
    if latest:
        notice_target = latest if dry_run else (new_history / latest.name)
        append_migration_notice(notice_target, old_resolved, new_resolved, dry_run)

    if delete_history:
        print()
        if dry_run:
            print(f"  Would remove old history dir: {old_history}")
        else:
            shutil.rmtree(old_history)
            print(f"  Removed old history dir: {old_history}")

    if delete_dir:
        print()
        delete_folder(old_resolved, dry_run=dry_run)

    print(f"\n{prefix}Done.")
    return 0


# ---------------------------------------------------------------------------
# rm
# ---------------------------------------------------------------------------


def remove(path: str, *, dry_run: bool = False, folder: bool = False) -> int:
    resolved = str(Path(path).resolve())
    history = PROJECTS_DIR / encode_path(path)
    prefix = "[DRY RUN] " if dry_run else ""

    print(f"{prefix}Removing Claude Code history for:")
    print(f"  {resolved}")
    print(f"  {history}")
    print()

    if history.is_dir():
        n_files = sum(1 for f in history.rglob("*") if f.is_file())
        if dry_run:
            print(f"  Would remove {n_files} history files")
        else:
            shutil.rmtree(history)
            print(f"  Removed {n_files} history files")
    else:
        print(f"  No history found at {history}")
        if not folder:
            return 1

    if folder:
        print()
        rc = delete_folder(resolved, dry_run=dry_run)
        if rc != 0 and not history.is_dir():
            return 1

    print(f"\n{prefix}Done.")
    return 0


def uuid_spine(path: Path) -> list[str]:
    """The ordered uuids of a transcript — its identity, independent of byte content.

    Transcripts are append-only, so one session's spine being a prefix of another's
    means the longer file is literally the same conversation, continued. Unlike a byte
    compare this survives path rewriting: an imported transcript has every embedded
    path replaced, so it shares not one byte with its own earlier copy while remaining
    the same conversation. That is why a plain prefix test reports 'conflict' on files
    that only differ by the rewrite.
    """
    spine: list[str] = []
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                try:
                    val = json.loads(line).get("uuid")
                except (json.JSONDecodeError, AttributeError):
                    continue
                if val:
                    spine.append(val)
    except OSError:
        return []
    return spine


def smart_merge_file(src: Path, dst: Path) -> str:
    """Merge a single file. Returns: 'added', 'identical', 'upgraded', 'kept', or 'conflict'."""
    if not dst.exists():
        shutil.copy2(src, dst)
        return "added"

    if filecmp.cmp(src, dst, shallow=False):
        return "identical"

    if src.suffix == ".jsonl":
        src_size = src.stat().st_size
        dst_size = dst.stat().st_size
        if src_size > dst_size:
            with open(dst, "rb") as d, open(src, "rb") as s:
                existing = d.read()
                if s.read(len(existing)) == existing:
                    shutil.copy2(src, dst)
                    return "upgraded"
        elif dst_size > src_size:
            with open(src, "rb") as s, open(dst, "rb") as d:
                incoming = s.read()
                if d.read(len(incoming)) == incoming:
                    return "kept"

        # Same test, on the uuid spine instead of raw bytes, so it still fires after
        # the paths have been rewritten. A strictly shorter transcript whose spine is
        # a prefix of the other is an earlier state of that same session: the longer
        # file already contains it exactly, so keeping both would only ever mean
        # keeping a stale copy. Drop the shorter one.
        # Equal-length spines are NOT a proper subset - the two sides diverged, or
        # differ only by the rewrite - so those still become a conflict for a human.
        src_spine, dst_spine = uuid_spine(src), uuid_spine(dst)
        if src_spine and dst_spine:
            if len(src_spine) > len(dst_spine) and src_spine[:len(dst_spine)] == dst_spine:
                shutil.copy2(src, dst)
                return "upgraded"
            if len(dst_spine) > len(src_spine) and dst_spine[:len(src_spine)] == src_spine:
                return "kept"

        shutil.copy2(src, Path(str(dst) + ".incoming"))
        return "conflict"

    if src.suffix == ".json":
        if src.stat().st_mtime > dst.stat().st_mtime:
            shutil.copy2(src, dst)
            return "upgraded"
        return "kept"

    shutil.copy2(src, Path(str(dst) + ".incoming"))
    return "conflict"


def rewrite_paths(directory: Path, old_path: str, new_path: str) -> None:
    """Replace all occurrences of old_path with new_path in JSON/JSONL files."""
    for f in directory.rglob("*"):
        if f.is_file() and f.suffix in (".json", ".jsonl"):
            content = f.read_text()
            if old_path in content:
                f.write_text(content.replace(old_path, new_path))
# ---------------------------------------------------------------------------
# export / import (single project)
# ---------------------------------------------------------------------------


def export_history(project_path: str, output_dir: str = ".", *, dry_run: bool = False) -> int:
    resolved = str(Path(project_path).resolve())
    encoded = encode_path(project_path)
    history_dir = PROJECTS_DIR / encoded

    if not history_dir.is_dir():
        print(f"No history found for {resolved}")
        print(f"  (looked in {history_dir})")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        meta = {
            "kind": "claude-migrate-export",
            "original_path": resolved,
            "encoded_name": encoded,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "hostname": platform.node(),
            "user": getpass.getuser(),
        }
        (staging / "migrate-meta.json").write_text(json.dumps(meta, indent=2))

        # NEVER ship .incoming files. They are local merge artifacts, not history, and
        # they live inside the history directory - so exporting them sends a conflict to
        # the other machine, where it conflicts again and comes back as
        # .incoming.incoming, one level deeper per run, until the filename passes 255
        # bytes and every import for the project dies with Errno 36. Observed live at 9
        # levels and 75 files across 10 projects.
        shutil.copytree(history_dir, staging / "project-history",
                        ignore=shutil.ignore_patterns("*.incoming*"))

        sessions_dir = staging / "sessions"
        sessions_dir.mkdir()
        for f in (CLAUDE_DIR / "sessions").glob("*.json"):
            try:
                data = json.loads(f.read_text())
                if data.get("cwd", "").startswith(resolved):
                    shutil.copy2(f, sessions_dir / f.name)
            except (json.JSONDecodeError, OSError):
                pass

        output = Path(output_dir)
        basename = Path(resolved).name
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_path = output / f"claude-history-{basename}-{timestamp}.tar.gz"
        if dry_run:
            n = sum(1 for p in staging.rglob("*") if p.is_file())
            print(f"[DRY RUN] Would archive {n} files to {archive_path}")
        else:
            output.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(str(staging), arcname=".")
            size_kb = archive_path.stat().st_size / 1024
            print(f"Exported to: {archive_path} ({size_kb:.1f} KB)")
            print(f"\nTo import on another machine:")
            print(f"  uvx claude-migrate import {archive_path.name} <project_path>")
    return 0


def import_history(
    archive_path: str,
    target_path: str | None = None,
    *,
    dry_run: bool = False,
    userpath_value: str | None = None,
    replace_references: bool = False,
) -> int:
    archive = Path(archive_path)
    if not archive.is_file():
        print(f"Archive not found: {archive}")
        return 1

    target = str(Path(target_path).resolve()) if target_path else str(Path.cwd())
    encoded = encode_path(target)
    dest = PROJECTS_DIR / encoded

    with tempfile.TemporaryDirectory() as tmp:
        import_dir = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(import_dir, filter="data")

        meta_file = import_dir / "migrate-meta.json"
        original_path = ""
        if meta_file.exists():
            meta = json.loads(meta_file.read_text())
            original_path = meta.get("original_path", "")
            print(f"Source: {meta.get('hostname', 'unknown')} ({original_path})")
            print(f"Exported: {meta.get('exported_at', 'unknown')}")
        else:
            print("Warning: no metadata found in archive")

        src_dir = import_dir / "project-history"
        if not src_dir.is_dir():
            print("Error: archive missing project-history directory")
            return 1

        try:
            userpath_map = parse_userpath_flag(userpath_value, original_path or target, target)
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2

        # Default baseline rewrite: cross-path text replace (legacy behavior).
        if original_path and original_path != target:
            print(f"Rewriting paths: {original_path} -> {target}")
            rewrite_history_content(
                src_dir,
                original_path,
                target,
                userpath_map=userpath_map,
                replace_refs=replace_references or True,  # baseline rewrite on cross-path import
                dry_run=dry_run,
            )
            sessions_dir = import_dir / "sessions"
            if sessions_dir.is_dir():
                rewrite_history_content(
                    sessions_dir,
                    original_path,
                    target,
                    userpath_map=userpath_map,
                    replace_refs=replace_references or True,
                    dry_run=dry_run,
                )
        elif userpath_map or replace_references:
            rewrite_history_content(
                src_dir,
                original_path or target,
                target,
                userpath_map=userpath_map,
                replace_refs=replace_references,
                dry_run=dry_run,
            )

        stats = merge_trees(src_dir, dest, dry_run=dry_run)

        sessions_src = import_dir / "sessions"
        if sessions_src.is_dir():
            sessions_dst = CLAUDE_DIR / "sessions"
            sessions_dst.mkdir(parents=True, exist_ok=True)
            for f in sessions_src.glob("*"):
                if not (sessions_dst / f.name).exists():
                    if not dry_run:
                        shutil.copy2(f, sessions_dst / f.name)
                    stats["added"] += 1

        if not dry_run:
            print_merge_stats(stats)

        print(f"\nTarget: {dest}")
        print(f"You can now: cd {target} && claude --continue")
    return 0


# ---------------------------------------------------------------------------
# export-all / import-all (everything under ~/.claude/projects)
# ---------------------------------------------------------------------------


def export_all_history(output_dir: str = ".", *, dry_run: bool = False) -> int:
    if not PROJECTS_DIR.is_dir():
        print(f"No projects dir found at {PROJECTS_DIR}")
        return 1
    projects = sorted(p for p in PROJECTS_DIR.iterdir() if p.is_dir())
    if not projects:
        print(f"No project history found in {PROJECTS_DIR}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        staging = Path(tmp)
        meta = {
            "kind": "claude-migrate-export-all",
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "hostname": platform.node(),
            "user": getpass.getuser(),
            "claude_dir": str(CLAUDE_DIR),
            "projects": [p.name for p in projects],
        }
        (staging / "migrate-meta.json").write_text(json.dumps(meta, indent=2))

        projects_staging = staging / "projects"
        projects_staging.mkdir()
        for p in projects:
            shutil.copytree(p, projects_staging / p.name)

        sessions_src = CLAUDE_DIR / "sessions"
        if sessions_src.is_dir():
            shutil.copytree(sessions_src, staging / "sessions")

        output = Path(output_dir)
        timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        archive_path = output / f"claude-history-ALL-{platform.node()}-{timestamp}.tar.gz"

        if dry_run:
            n_files = sum(1 for p in staging.rglob("*") if p.is_file())
            print(f"[DRY RUN] Would archive {len(projects)} project(s), {n_files} files to {archive_path}")
        else:
            output.mkdir(parents=True, exist_ok=True)
            with tarfile.open(archive_path, "w:gz") as tar:
                tar.add(str(staging), arcname=".")
            size_mb = archive_path.stat().st_size / (1024 * 1024)
            print(f"Exported {len(projects)} project(s) to: {archive_path} ({size_mb:.1f} MB)")
            print(f"\nTo import on another machine:")
            print(f"  uvx claude-migrate import-all {archive_path.name}")
    return 0


def import_all_history(
    archive_path: str,
    *,
    dry_run: bool = False,
    userpath_value: str | None = None,
    replace_references: bool = False,
) -> int:
    archive = Path(archive_path)
    if not archive.is_file():
        print(f"Archive not found: {archive}")
        return 1

    with tempfile.TemporaryDirectory() as tmp:
        import_dir = Path(tmp)
        with tarfile.open(archive, "r:gz") as tar:
            tar.extractall(import_dir, filter="data")

        meta_file = import_dir / "migrate-meta.json"
        src_user = None
        if meta_file.exists():
            meta = json.loads(meta_file.read_text())
            if meta.get("kind") != "claude-migrate-export-all":
                print(f"Warning: expected kind='claude-migrate-export-all', got {meta.get('kind')!r}")
            src_user = meta.get("user")
            print(f"Source: {meta.get('hostname', 'unknown')} (user: {src_user or 'unknown'})")
            print(f"Projects in archive: {len(meta.get('projects', []))}")

        # Resolve userpath. Auto means: from = archive user; to = current user.
        userpath_map: tuple[str, str] | None = None
        try:
            if userpath_value == "auto":
                cur_user = getpass.getuser()
                if src_user and src_user != cur_user:
                    userpath_map = (src_user, cur_user)
            elif userpath_value:
                userpath_map = parse_userpath_flag(userpath_value, "", "")
        except ValueError as exc:
            print(f"ERROR: {exc}")
            return 2
        if userpath_map:
            print(f"User rewrite: {userpath_map[0]} -> {userpath_map[1]}")

        projects_src = import_dir / "projects"
        if not projects_src.is_dir():
            print("Archive missing projects/ dir")
            return 1

        total_stats = {"added": 0, "identical": 0, "upgraded": 0, "kept": 0, "conflict": 0}

        for proj_dir in sorted(projects_src.iterdir()):
            if not proj_dir.is_dir():
                continue
            # If user differs, the encoded dir name also needs to change.
            target_name = proj_dir.name
            if userpath_map:
                fu, tu = userpath_map
                for prefix in ("-Users-", "-home-"):
                    if proj_dir.name.startswith(f"{prefix}{fu}-"):
                        target_name = f"{prefix}{tu}-" + proj_dir.name[len(f"{prefix}{fu}-"):]
                        break

            # Content rewrite (the encoded form within content uses underscores in dir-name semantics;
            # rewrite_history_content already handles encoded forms).
            if userpath_map or replace_references:
                # We don't have a clean old/new CWD here — derive from encoded name if possible.
                old_cwd = "/" + proj_dir.name.lstrip("-").replace("-", "/")
                new_cwd = "/" + target_name.lstrip("-").replace("-", "/")
                rewrite_history_content(
                    proj_dir,
                    old_cwd,
                    new_cwd,
                    userpath_map=userpath_map,
                    replace_refs=replace_references,
                    dry_run=dry_run,
                )

            dest = PROJECTS_DIR / target_name
            stats = merge_trees(proj_dir, dest, dry_run=dry_run)
            for k, v in stats.items():
                total_stats[k] = total_stats.get(k, 0) + v

        sessions_src = import_dir / "sessions"
        if sessions_src.is_dir():
            sessions_dst = CLAUDE_DIR / "sessions"
            sessions_dst.mkdir(parents=True, exist_ok=True)
            for f in sessions_src.glob("*"):
                dst = sessions_dst / f.name
                if not dst.exists():
                    if not dry_run:
                        shutil.copy2(f, dst)
                    total_stats["added"] += 1

        if not dry_run:
            print_merge_stats(total_stats)
        print(f"\nImported into: {PROJECTS_DIR}")
    return 0


# ---------------------------------------------------------------------------
# user-messages: extract just the human-typed messages from the current chat
# ---------------------------------------------------------------------------


def find_current_session(cwd: str | None = None) -> Path | None:
    """Auto-detect the current session JSONL based on cwd + most-recent mtime."""
    cwd = cwd or os.getcwd()
    history_dir = PROJECTS_DIR / encode_path(cwd)
    if not history_dir.is_dir():
        return None
    sessions = sorted(history_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    return sessions[0] if sessions else None


def extract_user_messages(session_file: Path, *, include_meta: bool = False) -> list[str]:
    """Return human-typed user messages from a session JSONL.

    Skips:
    - assistant messages
    - tool_result blocks (which appear as type='user' but aren't human input)
    - sidechains (subagent invocations)
    - meta entries
    """
    out: list[str] = []
    for line in session_file.read_text().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if obj.get("type") != "user":
            continue
        if obj.get("isSidechain"):
            continue
        if obj.get("isMeta"):
            continue
        msg = obj.get("message", {})
        if msg.get("role") != "user":
            continue
        content = msg.get("content")
        text_parts: list[str] = []
        if isinstance(content, str):
            text_parts.append(content)
        elif isinstance(content, list):
            for block in content:
                if isinstance(block, dict) and block.get("type") == "text":
                    text_parts.append(block.get("text", ""))
        text = "\n".join(p for p in text_parts if p).strip()
        if not text:
            continue
        if include_meta:
            ts = obj.get("timestamp", "")
            out.append(f"[{ts}]\n{text}")
        else:
            out.append(text)
    return out


def copy_to_clipboard(text: str) -> str | None:
    """Copy text to system clipboard. Returns the tool name used, or None on failure."""
    import subprocess
    candidates: list[list[str]]
    if sys.platform == "darwin":
        candidates = [["pbcopy"]]
    elif sys.platform == "win32":
        candidates = [["clip"]]
    else:
        candidates = [
            ["wl-copy"],
            ["xclip", "-selection", "clipboard"],
            ["xsel", "--clipboard", "--input"],
        ]
    for cmd in candidates:
        try:
            p = subprocess.Popen(cmd, stdin=subprocess.PIPE)
            p.communicate(input=text.encode("utf-8"))
            if p.returncode == 0:
                return cmd[0]
        except FileNotFoundError:
            continue
    return None


def user_messages_cmd(
    *,
    session: str | None,
    copy: bool,
    output: str | None,
    include_meta: bool,
    separator: str,
) -> int:
    if session:
        session_file = Path(session)
        if not session_file.is_file():
            print(f"Session file not found: {session_file}", file=sys.stderr)
            return 1
    else:
        session_file = find_current_session()
        if not session_file:
            print(
                f"Could not auto-detect a session JSONL for cwd={os.getcwd()}.\n"
                f"  (looked under {PROJECTS_DIR / encode_path(os.getcwd())})\n"
                f"  Pass --session <file.jsonl> explicitly.",
                file=sys.stderr,
            )
            return 1

    messages = extract_user_messages(session_file, include_meta=include_meta)
    if not messages:
        print(f"No human-typed user messages found in {session_file}", file=sys.stderr)
        return 1

    blob = separator.join(messages)

    if output:
        Path(output).write_text(blob)
        print(f"Wrote {len(messages)} message(s) ({len(blob)} chars) to {output}", file=sys.stderr)
    elif copy:
        tool = copy_to_clipboard(blob)
        if tool:
            print(f"Copied {len(messages)} message(s) ({len(blob)} chars) to clipboard via {tool}", file=sys.stderr)
        else:
            print(
                "ERROR: no clipboard tool found "
                "(tried pbcopy / clip / wl-copy / xclip / xsel).\n"
                "Falling back to stdout — pipe into your own clipboard tool.",
                file=sys.stderr,
            )
            print(blob)
            return 1
    else:
        print(blob)
        print(f"\n[{len(messages)} message(s) from {session_file.name}]", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# install
# ---------------------------------------------------------------------------


def install() -> int:
    """Install /migrate as a SKILL, not a bare slash command.

    A skill carries its own reference files and triggers on description, so one artifact
    covers same-machine moves, cross-machine moves and the whole-setup case. Shipping
    both a command and a skill named `migrate` means two things answer to /migrate and
    they drift apart - which is exactly what happened before.
    """
    root = SKILLS_DIR / "migrate"
    src = importlib.resources.files("claude_migrate").joinpath("skill")
    for rel in SKILL_FILES:
        target = root / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(src.joinpath(rel).read_text())
        print(f"Installed {target}")

    # A stale command file shadows nothing but confuses everyone, including Claude.
    legacy = COMMANDS_DIR / "migrate.md"
    if legacy.exists():
        print(f"\n⚠ {legacy} also exists and now duplicates this skill.")
        print("  Remove it so /migrate resolves to one thing:")
        print(f"    rm {legacy}")

    print("\nUsage in Claude Code: /migrate <new_path> | [user@]host:<new_path>")
    return 0


# ---------------------------------------------------------------------------
# CLI dispatch
# ---------------------------------------------------------------------------


def _add_migrate_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument("old_path", help="Original project directory path")
    p.add_argument("new_path", help="New project directory path")
    p.add_argument("--merge", "-m", action="store_true",
                   help="Smart-merge if destination already has history")
    p.add_argument("--folder", action="store_true",
                   help="Also operate on the actual project folder (not just history)")
    p.add_argument("--delete-history", action="store_true",
                   help="Delete the source history after copying (move semantics)")
    p.add_argument("--delete-dir", action="store_true",
                   help="Delete the source project folder after copying (requires --folder)")
    p.add_argument("--replace-userpath", nargs="?", const="auto", default=None, metavar="FROM:TO",
                   help="Rewrite user segment in path references inside chat content. "
                        "Bare flag auto-detects from /Users/<x> or /home/<x>; use "
                        "--replace-userpath=alice:bob to override.")
    p.add_argument("--replace-references", action="store_true",
                   help="Rewrite absolute and relative path references inside chat content. "
                        "Absolute paths under the old CWD are rewritten under the new CWD. "
                        "Relative paths escaping the old CWD are pinned to their original absolute.")
    p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")


def _migrate_from_args(args, *, force_delete_history: bool = False) -> int:
    return migrate(
        args.old_path,
        args.new_path,
        dry_run=args.dry_run,
        merge=args.merge,
        folder=args.folder,
        delete_history=args.delete_history or force_delete_history,
        delete_dir=args.delete_dir,
        userpath_value=args.replace_userpath,
        replace_references=args.replace_references,
    )


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code conversation history migration tool."
    )
    sub = parser.add_subparsers(dest="command")

    mg = sub.add_parser(
        "migrate",
        help="Migrate Claude Code history (and optionally the project folder) between paths",
    )
    _add_migrate_flags(mg)

    cp_p = sub.add_parser("cp", help="[deprecated alias of `migrate`] Copy history (keeps original)")
    _add_migrate_flags(cp_p)

    mv_p = sub.add_parser(
        "mv",
        help="[deprecated alias of `migrate --delete-history`] Move history (removes original)",
    )
    _add_migrate_flags(mv_p)

    rm_p = sub.add_parser("rm", help="Remove history for a project directory")
    rm_p.add_argument("path", help="Project directory path whose history to remove")
    rm_p.add_argument("--folder", action="store_true",
                      help="Also delete the actual project folder, not just the history")
    rm_p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")

    ex_p = sub.add_parser("export", help="Export project history to a portable .tar.gz archive")
    ex_p.add_argument("project_path", nargs="?", default=".", help="Project directory (default: cwd)")
    ex_p.add_argument("-o", "--output", default=".", help="Output directory for the archive (default: cwd)")
    ex_p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")

    ea_p = sub.add_parser("export-all", help="Export ALL project histories under ~/.claude/projects/ to one archive")
    ea_p.add_argument("-o", "--output", default=".", help="Output directory for the archive (default: cwd)")
    ea_p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")

    im_p = sub.add_parser("import", aliases=["merge"], help="Import and smart-merge history from an archive")
    im_p.add_argument("archive", help="Path to .tar.gz archive")
    im_p.add_argument("target_path", nargs="?", help="Target project directory (default: cwd)")
    im_p.add_argument("--replace-userpath", nargs="?", const="auto", default=None, metavar="FROM:TO",
                      help="Rewrite user segment in path references (auto-detect by default)")
    im_p.add_argument("--replace-references", action="store_true",
                      help="Thorough rewrite of absolute and relative path references in content")
    im_p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")

    sy_p = sub.add_parser("sync", help="Two-way sync of history with peers in ~/.claude/history-sync.json")
    sy_p.add_argument("--interval", type=int, metavar="SEC", help="Loop forever, every SEC seconds")
    sy_p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")
    sy_p.add_argument("--verbose", "-v", action="store_true", help="Report peers with nothing to do")

    sub.add_parser("install-skill", aliases=["install-slash-command"],
                   help="Install the /migrate skill into ~/.claude/skills (or use the plugin marketplace)")

    ia_p = sub.add_parser("import-all", help="Import an export-all archive into ~/.claude/projects/")
    ia_p.add_argument("archive", help="Path to .tar.gz archive")
    ia_p.add_argument("--replace-userpath", nargs="?", const="auto", default=None, metavar="FROM:TO",
                      help="Rewrite user segment in encoded dir names and content")
    ia_p.add_argument("--replace-references", action="store_true",
                      help="Rewrite absolute and relative path references in content")
    ia_p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")

    um_p = sub.add_parser(
        "user-messages",
        help="Extract just the human-typed messages from the current chat session",
    )
    um_p.add_argument("--session", default=None,
                      help="Session JSONL file (default: auto-detect from cwd → most-recent)")
    um_p.add_argument("--copy", action="store_true",
                      help="Copy to clipboard (pbcopy/clip/wl-copy/xclip/xsel)")
    um_p.add_argument("-o", "--output", default=None, help="Write to a file instead of stdout")
    um_p.add_argument("--include-meta", action="store_true",
                      help="Prefix each message with its timestamp")
    um_p.add_argument("--separator", default="\n\n---\n\n",
                      help="Separator between messages (default: '\\n\\n---\\n\\n')")

    args = parser.parse_args()

    if args.command == "migrate":
        sys.exit(_migrate_from_args(args))
    elif args.command == "cp":
        print("note: `cp` is a deprecated alias; use `claude-migrate migrate` instead.", file=sys.stderr)
        sys.exit(_migrate_from_args(args))
    elif args.command == "mv":
        print("note: `mv` is a deprecated alias; use `claude-migrate migrate --delete-history` instead.", file=sys.stderr)
        sys.exit(_migrate_from_args(args, force_delete_history=True))
    elif args.command == "rm":
        sys.exit(remove(args.path, dry_run=args.dry_run, folder=args.folder))
    elif args.command == "export":
        sys.exit(export_history(args.project_path, args.output, dry_run=args.dry_run))
    elif args.command == "export-all":
        sys.exit(export_all_history(args.output, dry_run=args.dry_run))
    elif args.command in ("import", "merge"):
        sys.exit(import_history(
            args.archive, args.target_path,
            dry_run=args.dry_run,
            userpath_value=args.replace_userpath,
            replace_references=args.replace_references,
        ))
    elif args.command == "import-all":
        sys.exit(import_all_history(
            args.archive,
            dry_run=args.dry_run,
            userpath_value=args.replace_userpath,
            replace_references=args.replace_references,
        ))
    elif args.command == "sync":
        # Imported lazily: sync needs fcntl, which does not exist on Windows.
        # A top-level import would break every OTHER subcommand there too.
        from claude_migrate import sync
        sys.exit(sync.run(args))
    elif args.command == "user-messages":
        sys.exit(user_messages_cmd(
            session=args.session,
            copy=args.copy,
            output=args.output,
            include_meta=args.include_meta,
            separator=args.separator,
        ))
    elif args.command in ("install-skill", "install-slash-command"):
        sys.exit(install())
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
