# hindsight-installer-mcp

**MCP server for managing Postgres extensions and embedding pipelines on [Hindsight](https://github.com/vectorize-io/hindsight) — works with any MCP-compatible agent.**

Install vchord. Swap embedding models. Migrate indexes. Verify recall. All without admin gymnastics for users on `pg0` (embedded Postgres) mode, and with a single elevation prompt for users on system Postgres.

## Why

Hindsight is great. Upgrading its embedding pipeline (e.g., swapping `BAAI/bge-small-en-v1.5` for `Qwen/Qwen3-Embedding-4B`) requires a coordinated dance: stop the API, swap env vars, ALTER the embedding column dimension, install vector extensions like vchord if you exceed pgvector's 2000-dim HNSW limit, restart, re-embed every memory, verify recall didn't break. Doing that by hand is fiddly and error-prone. Doing it via your AI agent should be one prompt.

This server gives any MCP-compatible agent (Claude Code, Cursor, Cline, Continue, OpenAI Codex, Gemini CLI, custom Anthropic SDK clients, etc.) the tools to do it safely.

## What it does

| Tool | Purpose |
|---|---|
| `detect_postgres_mode` | Returns whether you're on pg0 (user-space, no admin) or system Postgres (admin needed); reports lib_dir / share_dir / data_dir |
| `list_extensions` | What's currently installed in the Hindsight DB |
| `install_extension` | Auto-routes to no-admin pg0 install OR elevated system install (gsudo / sudo) |
| `uninstall_extension` | Remove an extension's files (after `DROP EXTENSION` in psql) |
| `get_hindsight_env` / `set_hindsight_env` | Read or update Hindsight's `.env` |
| `hindsight_status` | Is Hindsight running, what's the embedding column type, how many rows are embedded |
| `migrate_embeddings` | Wraps `hindsight-admin reindex-embeddings` with optional auto-backup + verify-recall |
| `verify_recall` | Standalone recall self-match sanity check |
| `upgrade_embedding_pipeline` | Orchestrator: backup → set env → emit ALTER SQL → migrate → verify |
| `check_hindsight_update` | Compare installed vs latest Hindsight, consult compat manifest, advise |
| `validate_compatibility` | CLI surface + dry-run + API health probes before upgrading |
| `safe_upgrade_hindsight` | Backup → pre-flight → pip upgrade → post-flight verify → rollback on failure |

## Install

```bash
pip install hindsight-installer-mcp
# or
uv add hindsight-installer-mcp
```

## Use it from your agent

### Claude Code (or any Claude Agent SDK client)

In `~/.claude.json` or your project's MCP config:

```json
{
  "mcpServers": {
    "hindsight-installer": {
      "command": "hindsight-installer-mcp"
    }
  }
}
```

### Cursor / Continue / Cline / Codex / Gemini CLI

Whatever the host's MCP config syntax is, the command stays the same: `hindsight-installer-mcp`. It speaks standard stdio MCP. Any agent that lists `tools/list` and `tools/call` against an MCP server can use it.

## Example workflows

### Upgrade Hindsight to Qwen3-Embedding-4B with vchord (Windows + pg0)

```
agent prompt: "Upgrade my Hindsight embedding to Qwen3-Embedding-4B at 2560 dim,
              using vchord for the vector index. Backup first."
```

The agent calls:

```
detect_postgres_mode()
  → mode: "pg0", elevation_required: false
install_extension(name="vchord", dll_or_so_path="...", sql_file_path="...", control_file_path="...")
  → ok: true, no admin needed
upgrade_embedding_pipeline(target_model="Qwen/Qwen3-Embedding-4B",
                          target_dimension=2560,
                          backup_path="./pre-upgrade.zip",
                          use_vchord=true,
                          trust_remote_code=true)
  → returns ALTER SQL the user runs in psql
migrate_embeddings(auto_backup_path="./pre-reembed.zip", verify_recall=true)
  → re-embeds every memory, verifies 5/5 self-match
```

### Same thing on system Postgres (single elevation prompt)

```
detect_postgres_mode() → mode: "system", elevation_required: true
install_extension(...) → uses gsudo if installed, falls back to native sudo
                         (Windows 11 24H2+ inline mode is best, gsudo cache mode also works)
[rest is identical]
```

## Elevation handling

When `mode: "system"` and `elevation_required: true`:

- **Windows:** prefers `gsudo` ([gerardog/gsudo](https://github.com/gerardog/gsudo)) for cached-credential elevation; falls back to Windows 11 24H2+ native `sudo`. If neither is configured for non-interactive use, install fails with a clear message instead of hanging.
- **Linux / macOS:** uses `sudo`. If no TTY is available and sudo isn't NOPASSWD-configured for your user, install fails with a clear message.

When `mode: "pg0"`: no elevation ever — pg0 lives entirely in `~/.pg0/`, fully user-writable.

## Why a separate MCP, not an upstream contribution

Hindsight has its own admin CLI ([`hindsight-admin`](https://github.com/vectorize-io/hindsight/blob/main/hindsight-api-slim/hindsight_api/admin/cli.py)). Some of these tools (`migrate_embeddings`, `verify_recall`) are thin wrappers around it. The pieces that *don't* belong upstream — extension install with elevation routing, env-file mutation, agent-friendly orchestration — live here. This MCP is the agent surface; upstream Hindsight is the engine.

## Safe-upgrade workflow

When Hindsight ships a new version, your agent can do the whole "is this safe?" dance for you:

```
agent prompt: "Is there a Hindsight update? If so, upgrade safely."
```

1. `check_hindsight_update` — fetches latest from PyPI, consults the published [compat.json](./compat.json) matrix. Returns: status (ok / warn / block), advisories, recommendation.
2. `validate_compatibility` — pre-flight probes: are the CLI flags this MCP relies on still present (`--auto-backup`, `--verify-recall`, etc.)? Does `reindex-embeddings --dry-run` succeed against your DB? Is the API healthy?
3. `safe_upgrade_hindsight` — full reversible upgrade: backup → pre-flight → `pip install -U` → post-flight → if anything fails, `pip install old-version` rollback. Backup file remains regardless.

The compat matrix lives in this repo at [compat.json](./compat.json) and is fetched from raw GitHub at runtime — so updates to "what's compatible" can be PR'd independently of MCP code releases.

## Status & maintenance

**v0.2 — alpha, lazily maintained.** Core install + upgrade workflows tested on Windows 11 + Postgres 17.

This is a side-project glue layer between agents and Hindsight. I patch it when it bites me; PRs welcome but I'm not on a release schedule. If something breaks for you and there's no obvious fix:

- Open an issue with full context (Hindsight version, OS, traceback)
- I'll get to it eventually, but no SLA
- Or fork it — MIT license, that's literally what it's for

If Hindsight ever bumps a CLI flag that breaks `migrate_embeddings`, expect a patch within a week or two of me noticing. If you need an immediate fix, the wrapper is ~600 lines of single-file Python — easy to fork-and-patch.

PRs welcome for: Linux / macOS edge cases, additional extension installers, halfvec auto-detect, gsudo-on-non-Windows, additional Hindsight workflow tools.

## License

MIT. Do whatever — this is glue between your agent and your Postgres install.

## Related

- [vectorize-io/hindsight](https://github.com/vectorize-io/hindsight) — the engine
- [vectorize-io/hindsight#1258](https://github.com/vectorize-io/hindsight/pull/1258) — the `reindex-embeddings` admin command this MCP wraps
- [grimmjoww/vchord-windows-port](https://github.com/grimmjoww/vchord-windows-port) — Windows-native vchord build (the canonical extension example)
- [TensorChord/VectorChord](https://github.com/tensorchord/VectorChord) — the high-dimensional vector index extension
