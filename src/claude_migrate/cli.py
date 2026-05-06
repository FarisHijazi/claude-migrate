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
import importlib.resources
import json
import os
import platform
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
SLASH_COMMAND = importlib.resources.files("claude_migrate").joinpath("migrate.md").read_text()


def encode_path(p: str) -> str:
    """Encode a directory path the way Claude Code does: replace / and . with -."""
    return str(Path(p).resolve()).replace("/", "-").replace(".", "-")


def find_latest_session(history_dir: Path) -> Path | None:
    sessions = sorted(history_dir.glob("*.jsonl"), key=lambda f: f.stat().st_mtime, reverse=True)
    return sessions[0] if sessions else None


def append_migration_notice(session_file: Path, old_path: str, new_path: str, dry_run: bool) -> bool:
    lines = session_file.read_text().strip().split("\n")
    session_id = None
    last_uuid = None
    for line in reversed(lines):
        obj = json.loads(line)
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
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": notice}],
        },
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


def migrate(old_path: str, new_path: str, *, dry_run: bool = False, delete_old: bool = False) -> int:
    old_resolved = str(Path(old_path).resolve())
    new_resolved = str(Path(new_path).resolve())
    old_history = PROJECTS_DIR / encode_path(old_path)
    new_history = PROJECTS_DIR / encode_path(new_path)
    action = "Moving" if delete_old else "Copying"
    prefix = "[DRY RUN] " if dry_run else ""

    print(f"{prefix}{action} Claude Code history:")
    print(f"  {old_resolved} -> {new_resolved}")
    print(f"  {old_history}")
    print(f"  {new_history}")
    print()

    if not old_history.is_dir():
        print(f"  No history found at {old_history}")
        return 1

    if new_history.exists():
        print(f"  WARNING: {new_history} already exists, skipping to avoid data loss.")
        return 1

    n_files = sum(1 for f in old_history.rglob("*") if f.is_file())
    if dry_run:
        print(f"  Would copy {n_files} files")
    else:
        shutil.copytree(old_history, new_history)
        print(f"  Copied {n_files} files")

    target = new_history if not dry_run else old_history
    latest = find_latest_session(target)
    if latest:
        notice_target = latest if dry_run else new_history / latest.name
        append_migration_notice(notice_target, old_resolved, new_resolved, dry_run)

    if delete_old:
        if dry_run:
            print(f"  Would remove old history dir")
        else:
            shutil.rmtree(old_history)
            print(f"  Removed old history dir")

    print(f"\n{prefix}Done.")
    return 0


def remove(path: str, *, dry_run: bool = False) -> int:
    resolved = str(Path(path).resolve())
    history = PROJECTS_DIR / encode_path(path)
    prefix = "[DRY RUN] " if dry_run else ""

    print(f"{prefix}Removing Claude Code history for:")
    print(f"  {resolved}")
    print(f"  {history}")
    print()

    if not history.is_dir():
        print(f"  No history found at {history}")
        return 1

    n_files = sum(1 for f in history.rglob("*") if f.is_file())
    if dry_run:
        print(f"  Would remove {n_files} files")
    else:
        shutil.rmtree(history)
        print(f"  Removed {n_files} files")

    print(f"\n{prefix}Done.")
    return 0


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
            "original_path": resolved,
            "encoded_name": encoded,
            "exported_at": datetime.now(timezone.utc).isoformat(),
            "hostname": platform.node(),
        }
        (staging / "migrate-meta.json").write_text(json.dumps(meta, indent=2))

        shutil.copytree(history_dir, staging / "project-history")

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


def import_history(archive_path: str, target_path: str | None = None, *, dry_run: bool = False) -> int:
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
            tar.extractall(import_dir)

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

        if original_path and original_path != target:
            print(f"Rewriting paths: {original_path} -> {target}")
            rewrite_paths(src_dir, original_path, target)
            sessions_dir = import_dir / "sessions"
            if sessions_dir.is_dir():
                rewrite_paths(sessions_dir, original_path, target)

        dest.mkdir(parents=True, exist_ok=True)
        stats: dict[str, int] = {"added": 0, "identical": 0, "upgraded": 0, "kept": 0, "conflict": 0}

        for src_file in src_dir.rglob("*"):
            if not src_file.is_file():
                continue
            rel = src_file.relative_to(src_dir)
            dst_file = dest / rel
            dst_file.parent.mkdir(parents=True, exist_ok=True)

            if dry_run:
                print(f"  [DRY RUN] would merge: {rel}")
            else:
                status = smart_merge_file(src_file, dst_file)
                stats[status] += 1
                if status not in ("identical", "kept"):
                    print(f"  {status}: {rel}")

        sessions_src = import_dir / "sessions"
        if sessions_src.is_dir():
            sessions_dst = CLAUDE_DIR / "sessions"
            sessions_dst.mkdir(parents=True, exist_ok=True)
            for f in sessions_src.glob("*"):
                if not (sessions_dst / f.name).exists():
                    shutil.copy2(f, sessions_dst / f.name)
                    stats["added"] += 1

        if not dry_run:
            print(f"\nMerge result:")
            for k, v in stats.items():
                if v:
                    print(f"  {k}: {v}")
            if stats["conflict"]:
                print(f"\n⚠ {stats['conflict']} conflict(s) saved as .incoming files")

        print(f"\nTarget: {dest}")
        print(f"You can now: cd {target} && claude --continue")

    return 0


def install() -> int:
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    target = COMMANDS_DIR / "migrate.md"
    target.write_text(SLASH_COMMAND)
    print(f"Installed /migrate slash command to {target}")
    print("Usage in Claude Code: /migrate <new_path>")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code conversation history migration tool."
    )
    sub = parser.add_subparsers(dest="command")

    cp_p = sub.add_parser("cp", help="Copy history to match a moved project directory (keeps original)")
    cp_p.add_argument("old_path", help="Original project directory path")
    cp_p.add_argument("new_path", help="New project directory path")
    cp_p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")

    mv_p = sub.add_parser("mv", help="Move history to match a moved project directory (removes original)")
    mv_p.add_argument("old_path", help="Original project directory path")
    mv_p.add_argument("new_path", help="New project directory path")
    mv_p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")

    rm_p = sub.add_parser("rm", help="Remove history for a project directory")
    rm_p.add_argument("path", help="Project directory path whose history to remove")
    rm_p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")

    ex_p = sub.add_parser("export", help="Export project history to a portable .tar.gz archive")
    ex_p.add_argument("project_path", nargs="?", default=".", help="Project directory (default: cwd)")
    ex_p.add_argument("-o", "--output", default=".", help="Output directory for the archive (default: cwd)")
    ex_p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")

    im_p = sub.add_parser("import", aliases=["merge"], help="Import and smart-merge history from an archive")
    im_p.add_argument("archive", help="Path to .tar.gz archive")
    im_p.add_argument("target_path", nargs="?", help="Target project directory (default: cwd)")
    im_p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")

    sub.add_parser("install-slash-command", help="Install the /migrate slash command for Claude Code")

    args = parser.parse_args()
    if args.command == "cp":
        sys.exit(migrate(args.old_path, args.new_path, dry_run=args.dry_run))
    elif args.command == "mv":
        sys.exit(migrate(args.old_path, args.new_path, dry_run=args.dry_run, delete_old=True))
    elif args.command == "rm":
        sys.exit(remove(args.path, dry_run=args.dry_run))
    elif args.command == "export":
        sys.exit(export_history(args.project_path, args.output, dry_run=args.dry_run))
    elif args.command in ("import", "merge"):
        sys.exit(import_history(args.archive, args.target_path, dry_run=args.dry_run))
    elif args.command == "install-slash-command":
        sys.exit(install())
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
