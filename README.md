# hindsight-installer-mcp

> Agent-safe tooling for Hindsight embedding migrations, Postgres extension management, compatibility checks, recall verification, backup, and rollback.

`hindsight-installer-mcp` gives MCP-compatible agents a controlled way to maintain a [Hindsight](https://github.com/vectorize-io/hindsight) memory stack instead of asking a user to manually coordinate database changes, environment settings, re-embedding, and recovery.

## The problem it solves

Changing an embedding model in a populated memory system is not a one-line configuration edit. A safe migration can require the agent to detect the Postgres mode, install a vector extension, stop services, back up data, change dimensions, rebuild embeddings and indexes, verify recall, and roll back when a compatibility check fails.

This project turns that fragile sequence into inspectable MCP tools and orchestrated workflows.

## What it provides

| Tool | Purpose |
|---|---|
| `detect_postgres_mode` | Detect embedded `pg0` versus system Postgres and report the relevant directories and elevation requirements. |
| `list_extensions` | Show extensions currently available to the Hindsight database. |
| `install_extension` / `uninstall_extension` | Manage Postgres extension files with mode-aware elevation handling. |
| `get_hindsight_env` / `set_hindsight_env` | Read and update Hindsight environment configuration. |
| `hindsight_status` | Report service health, embedding-column type, and embedded-row state. |
| `migrate_embeddings` | Run a reindex/re-embedding workflow with optional backup and recall verification. |
| `verify_recall` | Run a standalone self-match sanity check after a migration. |
| `upgrade_embedding_pipeline` | Coordinate backup, configuration, schema guidance, migration, and verification. |
| `check_hindsight_update` | Compare the installed Hindsight version against the latest release and compatibility data. |
| `validate_compatibility` | Probe required CLI surfaces, dry-run behavior, and API health before an upgrade. |
| `safe_upgrade_hindsight` | Back up, validate, upgrade, verify, and roll back when post-flight checks fail. |

## Why this is useful to agent builders

- **Tool-level control:** each operation has a narrow purpose rather than giving an agent unrestricted shell access.
- **Reversible workflows:** backups and rollback are part of the upgrade path, not an afterthought.
- **Environment awareness:** the server adapts to `pg0` and system Postgres instead of assuming one installation layout.
- **Evidence after action:** recall checks and compatibility probes give the agent something concrete to verify.
- **Cross-client support:** the stdio MCP surface can be used by Claude Code, Cursor, Cline, Continue, Codex, Gemini CLI, and custom MCP clients.

## Install

```bash
pip install hindsight-installer-mcp
# or
uv add hindsight-installer-mcp
```

Register the command in the MCP configuration used by your agent host:

```json
{
  "mcpServers": {
    "hindsight-installer": {
      "command": "hindsight-installer-mcp"
    }
  }
}
```

## Example: embedding upgrade with vchord

A user can ask an agent:

```text
Upgrade my Hindsight embedding model, use vchord for the vector index,
back up the current state first, and verify recall after migration.
```

A capable client can then call the narrow tools in sequence:

```text
detect_postgres_mode
→ install_extension
→ upgrade_embedding_pipeline
→ migrate_embeddings
→ verify_recall
```

The exact database and schema actions still depend on the installed Hindsight and Postgres versions. The compatibility and dry-run tools exist to detect that boundary before the agent changes the system.

## Elevation model

- **`pg0` mode:** files live in the user directory, so extension installation does not require administrator access.
- **System Postgres:** installation may require one UAC or `sudo` approval. On Windows the implementation prefers `gsudo`, then supported native `sudo` behavior, and fails clearly rather than hanging when non-interactive elevation is unavailable.

## Status

**v0.2 alpha.** Core installation and upgrade paths were exercised on Windows 11 and PostgreSQL 17. This is a focused integration project, not a hosted service and not an SLA-backed product. Review the compatibility matrix and run the provided checks before using it against valuable data.

## Portfolio notes

This repository demonstrates:

- MCP tool design for a real operational workflow;
- state inspection before mutation;
- human-elevation boundaries;
- backup, validation, and rollback planning;
- integration across an agent host, Hindsight, Postgres, vector extensions, and embedding providers;
- documentation built around the failure modes encountered during implementation.

The project was developed through an AI-assisted engineering workflow directed by **Willie Stewart / Phantom Horizon Studios**: defining the desired workflow and safety constraints, directing implementation, testing system behavior, diagnosing failures, and iterating on the integration and documentation.

## Related projects

- [vectorize-io/hindsight](https://github.com/vectorize-io/hindsight) — memory engine
- [grimmjoww/vchord-windows-port](https://github.com/grimmjoww/vchord-windows-port) — Windows-native VectorChord build and Hindsight migration notes
- [TensorChord/VectorChord](https://github.com/tensorchord/VectorChord) — vector index extension

## License

MIT. Hindsight, VectorChord, and other dependencies retain their own licenses.
