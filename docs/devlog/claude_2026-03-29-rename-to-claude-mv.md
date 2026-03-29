# Rename from claude-migrate to claude-mv

**Date:** 2026-03-29

## Changes

- Renamed source directory `src/claude_migrate/` to `src/claude_mv/`
- Updated `pyproject.toml`: package name and script entry point
- Updated `cli.py`: slash command content, install target filename (`mv.md`), printed text
- Updated `CLAUDE.md` and `README.md` with new naming
- Renamed global slash command from `/migrate` (`migrate.md`) to `/mv` (`mv.md`)
- Updated git remote to `git@github-personal:FarisHijazi/claude-mv.git`
- Reinstalled globally via `uv tool install`
