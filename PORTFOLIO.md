# Safe Hindsight Embedding Upgrade MCP

## The Problem

Upgrading an agent-memory system is rarely one setting change. A Hindsight embedding migration can involve stopping services, changing model and dimension settings, installing a different Postgres vector extension, altering the database schema, re-embedding existing memories, checking compatibility, and confirming that recall still works afterward.

That is manageable by hand once. It becomes risky when an AI agent is expected to perform the workflow repeatedly across different Windows and Postgres setups.

## What the System Does

`hindsight-installer-mcp` gives MCP-compatible agents a controlled tool surface for that work. The server can:

- detect whether Hindsight is using embedded `pg0` or system PostgreSQL
- inspect installed extensions and current embedding state
- install or remove Postgres extension files using the correct elevation path
- read and update Hindsight environment configuration
- migrate embeddings with optional backups
- run recall checks after a migration
- compare installed and available Hindsight versions
- validate the CLI and API behavior the integration depends on
- perform a reversible upgrade and roll back when post-upgrade checks fail

Representative tools include `detect_postgres_mode`, `install_extension`, `hindsight_status`, `upgrade_embedding_pipeline`, `migrate_embeddings`, `verify_recall`, `validate_compatibility`, and `safe_upgrade_hindsight`.

## Safety and Rollback Controls

The workflow was designed around the places an agent can do real damage:

- Postgres mode is detected before choosing an install path.
- System-level extension installation uses an explicit elevation route instead of silently assuming administrator access.
- Migration tools can create backups before re-embedding data.
- Compatibility checks probe required command-line behavior and API health instead of trusting version numbers alone.
- Safe upgrades include pre-flight and post-flight checks with package rollback when validation fails.
- Recall verification is a separate operation, so a successful process exit is not treated as proof that memory quality survived the migration.

## Representative Workflow

```text
Agent receives target model and dimension
        │
        ▼
Detect Postgres mode and current Hindsight state
        │
        ▼
Validate extension and version compatibility
        │
        ▼
Back up the current installation and memory data
        │
        ▼
Install the required vector extension
        │
        ▼
Update configuration and migrate embeddings
        │
        ▼
Verify recall and service health
        │
        ├── pass ──▶ keep the upgrade
        └── fail ──▶ restore the prior package/configuration
```

## Verification Evidence

The repository documents core install and upgrade workflows tested on Windows 11 with PostgreSQL 17. It also exposes dry-run, health, compatibility, and recall checks so the agent can produce evidence during a real migration instead of relying on a static setup guide.

The project is an **alpha integration**, not a promise that every Hindsight, operating-system, and Postgres combination has been validated.

## My Contribution

I directed the project from workflow definition through implementation and verification. That included identifying the manual failure points, defining the MCP tool boundaries, requiring backup and rollback behavior, reviewing repository changes, testing the Windows elevation and Postgres paths, and refining the flow when Hindsight’s actual CLI and environment behavior differed from assumptions.

The implementation was completed through an AI-assisted engineering workflow with human review of architecture, diffs, tests, and observed system behavior.

## Why This Matters to a Client

This project demonstrates the kind of work I am strongest at: taking a fragile, multi-system process and turning it into a tool-driven workflow an agent can execute, inspect, and recover from. The same approach applies to internal AI platforms, deployment assistants, migration tools, RAG systems, and agent workflows that need more than a happy-path demo.
