# claude-mv

Simple CLI tool to copy Claude Code conversation history when moving project directories.

## Structure

```
src/claude_mv/
  __init__.py    # empty
  cli.py         # all logic: encode_path, migrate, append_migration_notice, install
```

## Key detail

Claude Code encodes paths by replacing both `/` and `.` with `-`.

## Install

- `uvx claude-mv install` to set up the `/mv` slash command
- `uvx claude-mv copy <old> <new>` to run directly
- `uv sync` for local dev
