#!/usr/bin/env python3
"""Copy Claude Code conversation history to match a moved project directory.

When you move/copy a project directory, Claude Code loses track of its
conversation history because it's keyed by encoded absolute path.
This copies ~/.claude/projects/<old-encoded>/ to
~/.claude/projects/<new-encoded>/ and appends a migration notice to
the most recent session so Claude knows paths have changed.
"""

import argparse
import json
import shutil
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"
COMMANDS_DIR = Path.home() / ".claude" / "commands"

SLASH_COMMAND = """\
---
argument-hint: <new_path>
description: Migrate conversation history after moving this project to a new directory
---

# Migrate Claude Code History

The user has moved (or is about to move) this project to a new directory. Run the migration script to copy the conversation history so `claude --continue` works at the new location.

**Old path (current):** The current working directory (use `pwd`)
**New path:** $ARGUMENTS

Run this command:
```
uvx claude-mv copy "$(pwd)" "$ARGUMENTS"
```

After running, tell the user:
1. The history has been copied
2. They can now `cd $ARGUMENTS && claude --continue` to resume
"""


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


def migrate(old_path: str, new_path: str, *, dry_run: bool = False) -> int:
    old_resolved = str(Path(old_path).resolve())
    new_resolved = str(Path(new_path).resolve())
    old_history = PROJECTS_DIR / encode_path(old_path)
    new_history = PROJECTS_DIR / encode_path(new_path)
    prefix = "[DRY RUN] " if dry_run else ""

    print(f"{prefix}Copying Claude Code history:")
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

    print(f"\n{prefix}Done.")
    return 0


def install() -> int:
    COMMANDS_DIR.mkdir(parents=True, exist_ok=True)
    target = COMMANDS_DIR / "mv.md"
    target.write_text(SLASH_COMMAND)
    print(f"Installed /mv slash command to {target}")
    print("Usage in Claude Code: /mv <new_path>")
    return 0


def main():
    parser = argparse.ArgumentParser(
        description="Claude Code conversation history migration tool."
    )
    sub = parser.add_subparsers(dest="command")

    copy_p = sub.add_parser("copy", help="Copy conversation history to match a moved project directory")
    copy_p.add_argument("old_path", help="Original project directory path")
    copy_p.add_argument("new_path", help="New project directory path")
    copy_p.add_argument("--dry-run", "-n", action="store_true", help="Preview without making changes")

    sub.add_parser("install", help="Install the /mv slash command for Claude Code")

    args = parser.parse_args()
    if args.command == "copy":
        sys.exit(migrate(args.old_path, args.new_path, dry_run=args.dry_run))
    elif args.command == "install":
        sys.exit(install())
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
