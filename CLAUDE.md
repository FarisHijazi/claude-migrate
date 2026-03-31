# claude-migrate

Simple CLI tool to copy Claude Code conversation history when moving project directories.

## Structure

```
src/claude_migrate/
  __init__.py    # empty
  cli.py         # all logic: encode_path, migrate, append_migration_notice, install
```

## Key detail

Claude Code encodes paths by replacing both `/` and `.` with `-`.

## Install

- `uvx claude-migrate install-slash-command` to set up the `/migrate` slash command
- `uvx claude-migrate cp <old> <new>` to run directly
- `uv sync` for local dev
