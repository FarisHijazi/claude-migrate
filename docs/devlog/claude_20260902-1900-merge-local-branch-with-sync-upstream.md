# Merged the long-running local working tree with upstream `sync` (#3)

2026-09-02

## What happened

A large set of uncommitted local changes (969 lines in `cli.py`, plus `CLAUDE.md`,
`README.md`, `migrate.md`) had been sitting in the working tree while PR #3
(`ba147e2`, "sync subcommand, prefix-subset merge, and Claude Code plugin support")
landed upstream on the same files. Both sides had grown from `f407889`. The two were
reconciled here; `git stash` held the local side throughout.

## The important call: upstream owns merging, local owns rewriting

Both sides had independently written `smart_merge_file`, and git's merge left **two
definitions in the same file** — the later one silently winning. They are not
equivalent:

- The local version compares raw bytes. `import` rewrites every embedded path, so a
  transcript shares zero bytes with its own earlier copy and the byte prefix test
  reports `conflict` on files that are the same conversation.
- Upstream's compares the **uuid spine**, which survives that rewrite, and it has seven
  tests plus `.incoming` guards on both the export and merge sides (the compounding
  `.incoming.incoming` bug that ends in Errno 36).

The local duplicate was deleted. Upstream also won on `install()` (a skill, not a bundled
slash command) and on excluding `.incoming` from export tarballs.

The local side kept everything upstream did not have: `export-all` / `import-all`,
`user-messages`, and the whole content-rewriting layer (`--replace-userpath`,
`--replace-references`, `rewrite_history_content`, `transform_string`).

## `migrate.md` is gone, its content is not

Upstream deleted the packaged slash command in favour of `skill/SKILL.md`. The local
branch had meanwhile documented `--folder`, `--delete-history`, `--delete-dir`,
`--replace-userpath` and `--replace-references` in that file. Deleting it would have
dropped those, so they were ported into `skill/SKILL.md`, whose Step 1 now uses
`migrate` rather than the deprecated `cp`/`mv`.

## Result

11 subcommands: `migrate`, `cp`, `mv`, `rm`, `export`, `export-all`, `import`
(alias `merge`), `import-all`, `sync`, `user-messages`, `install-skill`
(alias `install-slash-command`).

## Also fixed

`tar.extractall()` was called without `filter=`, which Python 3.14 will reject outright
and which accepts absolute paths and escaping symlinks from a crafted archive. Both call
sites now pass `filter="data"`.

## Tests

`tests/cli_features_test.py` is new — the merged half had no coverage. It asserts that
`--replace-references` removes the old cwd from the transcript, that `--dry-run` writes
nothing, that `export-all`/`import-all` round-trips byte-for-byte, that
`extract_user_messages` drops assistant turns, sidechains and meta entries, and that
`install()` writes a skill and no command. 13 tests pass.
