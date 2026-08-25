# Platform notes

Per-OS specifics for the `migrate` skill. Read before the first copy.

- [Credential storage](#credential-storage)
- [What to copy and what to leave](#what-to-copy-and-what-to-leave)
- [rsync flavors](#rsync-flavors)
- [Recognising machine-specific payload](#recognising-machine-specific-payload)
- [Verified end-to-end example](#verified-end-to-end-example)

## Credential storage

| Source OS | Live credential | Read it with |
|---|---|---|
| macOS | **login Keychain**, service `Claude Code-credentials`, account = `$USER` | `security find-generic-password -s "Claude Code-credentials" -a "$USER" -w` |
| Linux | `~/.claude/.credentials.json` | plain file |
| Windows/WSL | `~/.claude/.credentials.json` | plain file |

On macOS **both** may exist and disagree; the on-disk file can be days old while
the Keychain is current. Always size-compare and prefer the Keychain. Targets
that are not macOS read the file, so a macOS→Linux migration means
Keychain → file.

Payload shape (inspect keys and expiries only — never print values):

```
claudeAiOauth.accessToken / refreshToken
claudeAiOauth.expiresAt              ms epoch — hours
claudeAiOauth.refreshTokenExpiresAt  ms epoch — weeks; THIS is the useful one
claudeAiOauth.subscriptionType
mcpOAuth.<server>|<hash>.*           per-MCP tokens, carried along
```

A short `expiresAt` is not a problem: it refreshes automatically as long as the
refresh token is valid. Report `refreshTokenExpiresAt` as the real deadline.

## What to copy and what to leave

Anchor every exclude with a leading `/` so it means "top level only". Without
the slash, `cache/` and `tmp/` also match `plugins/x/cache/` and `jobs/*/tmp/`.

| Path | Action | Why |
|---|---|---|
| `settings.json`, `CLAUDE.md`, `memory/` | copy | the actual configuration |
| `skills/`, `commands/`, `agents/`, `hooks/`, `scripts/` | copy | user extensions |
| `projects/` | **via `claude-migrate`** | path-keyed; see SKILL.md §3 |
| `history.jsonl`, `.git/`, `file-history/`, `jobs/` | copy | history the user asked for |
| `plugins/*.json` | **copy** | records which plugins to re-fetch |
| `/plugins/cache/`, `/plugins/marketplaces/` | **exclude** | downloaded payload, built for the source OS/arch |
| `node_modules/` (any depth) | exclude | native binaries; reinstalled per platform |
| `/shell-snapshots/` | exclude | snapshots of the source machine's shell |
| `/statsig/`, `/cache/`, `/paste-cache/`, `/tmp/` | exclude | regenerable |
| `.credentials.json` | exclude from the bulk copy | transferred separately, see §2 |
| `.DS_Store` | exclude | noise |

Excluding the plugin payload while keeping its JSON is deliberate: Claude Code
re-downloads the same plugin list natively on first run. Copying the payload
instead ships binaries that cannot execute.

## rsync flavors

macOS no longer ships GNU rsync. `rsync --version` reporting **`openrsync`** /
"protocol version 29" / "rsync version 2.6.9 compatible" means these are
**unavailable** and the command aborts with a usage dump:

- `--info=...`
- `--copy-unsafe-links`

Portable subset that works from macOS: `-a`, `-z`, `--delete`, `--exclude`,
`--stats`, `-e ssh`. Handle out-of-tree symlinks separately rather than reaching
for `--copy-unsafe-links`.

A multi-GB copy outlives a foreground tool timeout — run it in the background and
poll, rather than chaining sleeps.

## Recognising machine-specific payload

Before copying anything large, check what is compiled:

```bash
find ~/.claude \( -name '*.node' -o -name '*.dylib' -o -name '*.so' \) | head
```

Filenames carry the target triple — `darwin-arm64`, `x64-linux`, `win32`. If the
hits are all for the source platform, that subtree belongs on the exclude list.

## Verified end-to-end example

macOS (Apple Silicon) → Debian 13 x86-64, over LAN ssh. Roughly 2.0 GB after
exclusions, ~11k files, about ten minutes including verification.

What actually went wrong, in the order it surfaced:

1. `rsync --info=stats2` aborted — macOS `openrsync` does not have it.
2. First pass lost 134 files: `--exclude 'tmp/'` swallowed `jobs/*/tmp/`.
   Re-run with `/tmp/`.
3. The target had **no `node` at all**, so 9 of 14 hooks plus the statusline
   would have failed. Installed before first launch.
4. `.credentials.json` was 6 days stale (8034 B) versus the Keychain (8098 B).
   Copying the file would have silently failed to authenticate.
5. History was orphaned by the path change
   (`-Users-me-Projects-app` vs `-home-svc-Projects-app`) even though every byte
   had copied.
6. One Stop hook hardcoded an absolute source path; guarded it.
7. Three dangling symlinks, one of them inside `skills/`.

Every one of these is silent — the copy reports success in all seven cases.
That is why the verification ladder ends at `--continue` and concurrency rather
than at "files transferred".
