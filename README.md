# Hindsight Installer MCP

**Agent-callable tooling for safe Hindsight embedding upgrades, Postgres extension management, verification, and rollback.**

Changing an embedding model in a populated memory system is not a one-line configuration edit. The model, vector dimension, database column, index extension, environment, migration path, and recall quality all have to agree. Miss one piece and the system either refuses to start or quietly returns worse memories.

This MCP server gives an agent a controlled way to inspect that stack, make coordinated changes, verify the result, and recover when an upgrade fails.

## The problem it solves

A typical Hindsight embedding upgrade may require all of the following:

- Detect whether Hindsight is using embedded `pg0` or system Postgres
- Locate the correct Postgres library, extension, and data directories
- Install or remove a vector extension such as vchord
- Update Hindsight environment settings
- Reconcile embedding dimensions and index type
- Back up the current installation before migration
- Re-embed existing memories
- Check whether recall still returns the expected records
- Validate CLI and API compatibility before upgrading Hindsight itself
- Roll back the package when post-upgrade checks fail

Doing that by hand is easy to get wrong. This project turns the workflow into explicit MCP tools that Claude Code, Codex, Cursor, Cline, Gemini CLI, or another MCP-compatible agent can call step by step.

## Capability map

### Inspect

| Tool | Purpose |
|---|---|
| `detect_postgres_mode` | Detect `pg0`, system Postgres, or an unknown layout and report whether elevation is required |
| `list_extensions` | Show the Postgres extensions available to the Hindsight database |
| `get_hindsight_env` | Read relevant settings from the environment file and current process |
| `hindsight_status` | Report service state, embedding-column information, and embedded-row counts |
| `check_hindsight_update` | Compare the installed Hindsight version with the latest available version and consult the compatibility manifest |

### Change

| Tool | Purpose |
|---|---|
| `install_extension` | Route an extension install through user-space `pg0` or an elevated system-Postgres path |
| `uninstall_extension` | Remove extension files after the database extension has been dropped |
| `set_hindsight_env` | Add or replace a Hindsight environment setting without wiping unrelated values |
| `migrate_embeddings` | Run Hindsight's reindex workflow with optional backup and recall verification |
| `upgrade_embedding_pipeline` | Coordinate environment changes, extension selection, migration steps, and verification guidance |

### Verify and recover

| Tool | Purpose |
|---|---|
| `verify_recall` | Run a self-match sanity check against stored memories |
| `validate_compatibility` | Probe the CLI surface, dry-run behavior, and API health before a package upgrade |
| `safe_upgrade_hindsight` | Back up, run pre-flight checks, upgrade, run post-flight checks, and restore the prior version when validation fails |

## Safety model

This server is designed around reversible work rather than blind automation.

- `pg0` installs stay in user-writable space and do not require elevation.
- System Postgres installs use an explicit elevated path only when needed.
- Migration and package-upgrade flows can create backups before changing data or dependencies.
- Compatibility checks run before the upgrade instead of discovering broken flags halfway through it.
- Recall verification checks behavior after re-embedding, not merely whether the command exited successfully.
- The safe-upgrade path retains the backup and can reinstall the previous Hindsight version when post-flight checks fail.

The MCP does not make every database decision silently. When a schema change requires SQL that should be reviewed, the tool returns the required statement rather than pretending the risk does not exist.

## Install from source

The repository contains Python package metadata and a console entry point. A public PyPI release was not independently verified during this documentation pass, so the reliable installation path is the repository itself:

```bash
git clone https://github.com/grimmjoww/hindsight-installer-mcp.git
cd hindsight-installer-mcp
python -m venv .venv
python -m pip install -e .
```

Run the server:

```bash
hindsight-installer-mcp
```

## Connect it to an MCP host

For Claude Code:

```bash
claude mcp add --transport stdio hindsight-installer -- hindsight-installer-mcp
```

Then check the connection with:

```bash
claude mcp list
```

Other MCP hosts use their own configuration format, but the command remains `hindsight-installer-mcp` and the transport is stdio.

## Example: move a populated Hindsight install to vchord

A careful agent workflow looks like this:

```text
detect_postgres_mode()
  ↓
hindsight_status()
  ↓
install_extension(name="vchord", ...)
  ↓
upgrade_embedding_pipeline(
    target_model="Qwen/Qwen3-Embedding-4B",
    target_dimension=2560,
    backup_path="./pre-upgrade.zip",
    use_vchord=true,
    trust_remote_code=true
)
  ↓
review and run any returned ALTER statement
  ↓
migrate_embeddings(
    auto_backup_path="./pre-reembed.zip",
    verify_recall=true
)
```

The exact model and dimension are examples. The important part is the sequence: inspect first, back up, align the database and embedding configuration, migrate, then verify recall.

## Example: upgrade Hindsight without gambling on compatibility

```text
check_hindsight_update()
  ↓
validate_compatibility()
  ↓
safe_upgrade_hindsight()
```

The compatibility workflow checks the command-line flags and behavior this MCP depends on. The repository's [`compat.json`](./compat.json) file provides an independently updateable compatibility matrix for known Hindsight versions.

## Postgres modes and elevation

### Embedded `pg0`

When `HINDSIGHT_API_DATABASE_URL=pg0`, Postgres lives in the user's own directory. Extension installation stays in user space, so there is no UAC or `sudo` prompt.

### System Postgres

A system installation writes into protected Postgres directories. On Windows, the installer prefers `gsudo` and can fall back to supported native `sudo` behavior. On Linux and macOS, it uses `sudo` where available.

If elevation cannot run safely in the current environment, the tool should fail clearly rather than hang while waiting for an invisible password prompt.

## Tests and verification scope

The repository includes smoke tests for:

- Module and MCP entry-point imports
- Postgres-mode detection shape
- Structured environment inspection
- Rejection of missing extension files
- Safe environment-file insertion and replacement
- Version-pattern handling
- Offline-safe update-check result structure

Run them locally with:

```bash
python -m pip install pytest
python -m pytest -q
```

These are smoke tests, not full end-to-end database migration tests. The current repository status reports core install and upgrade workflows exercised on Windows 11 with Postgres 17. Linux and macOS paths are present, but this README does not claim the same depth of hands-on validation for every platform.

## Architecture boundary

Hindsight remains the memory engine. This repository is the agent-facing operational layer around it.

Some tools wrap existing `hindsight-admin` behavior. The original work here is the surrounding integration: Postgres-mode detection, extension installation with elevation routing, environment mutation, compatibility checks, agent-readable orchestration, verification, and rollback handling.

## Current status

**Version 0.2.0 — alpha.**

This is working integration software, not a managed service. There is no SLA, and future Hindsight CLI changes may require compatibility updates. The code is intentionally small enough to inspect and fork, and issues or pull requests with reproducible environment details are welcome.

Useful issue details include:

- Operating system and version
- Hindsight version
- Postgres or `pg0` mode
- Vector extension and embedding model
- Full traceback or tool result

## Related work

- [Hindsight](https://github.com/vectorize-io/hindsight) — the memory engine
- [Hindsight PR #1258](https://github.com/vectorize-io/hindsight/pull/1258) — the reindex command this integration wraps
- [VectorChord Windows Port](https://github.com/grimmjoww/vchord-windows-port) — native Windows build and Hindsight migration notes
- [VectorChord](https://github.com/tensorchord/VectorChord) — the vector-index extension
- [Phantom Horizon Studios](https://github.com/grimmjoww/phantom-horizons-studios) — related agent-systems work

## License

MIT.
