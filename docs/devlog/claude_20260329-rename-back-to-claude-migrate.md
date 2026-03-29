# Rename back to claude-migrate

**Date:** 2026-03-29

## What was done

Renamed the project from `claude-mv` back to `claude-migrate`:

1. Renamed GitHub repo via `gh repo rename`
2. Renamed `src/claude_mv/` to `src/claude_migrate/`
3. Updated `pyproject.toml` (package name and script entry point)
4. Updated `cli.py` (SLASH_COMMAND uvx reference, install() target filename and print messages)
5. Updated `CLAUDE.md` and `README.md` with new names
6. Replaced `/mv` slash command with `/migrate` in `~/.claude/commands/`
7. Renamed local directory from `claude-mv` to `claude-migrate`
8. Updated git remote URL
9. Reinstalled via `uv tool install`
