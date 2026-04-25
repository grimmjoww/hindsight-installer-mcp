"""
hindsight-installer-mcp — MCP server for managing Hindsight Postgres extensions
and embedding pipelines without admin gymnastics.

Tools provided:

  detect_postgres_mode  — pg0 (no admin) vs system Postgres
  list_extensions       — currently installed Postgres extensions
  install_extension     — install a built extension (auto-routes elevation)
  uninstall_extension   — remove an installed extension
  set_hindsight_env     — write key=value to a Hindsight .env file
  get_hindsight_env     — read current Hindsight env config
  hindsight_status      — is Hindsight running, what's the embedding state
  migrate_embeddings    — invoke `hindsight-admin reindex-embeddings`
  verify_recall         — sanity-check the embedding pipeline
  upgrade_embedding_pipeline — orchestrator: install ext → set env → restart → migrate → verify
  check_hindsight_update     — is a new Hindsight release out, is it compatible
  validate_compatibility     — pre-update health check + CLI surface diff
  safe_upgrade_hindsight     — backup → pre-flight → upgrade → verify → rollback on failure

Works with any MCP-compatible agent (Claude Code, Cursor, Cline, Continue, Codex, Gemini CLI, etc.).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP

mcp = FastMCP("hindsight-installer")


# ─── Config + helpers ─────────────────────────────────────────────────────────


def _is_windows() -> bool:
    return platform.system() == "Windows"


def _hindsight_url() -> str:
    """Resolve the Hindsight HTTP API URL from env or the default."""
    return os.environ.get("HINDSIGHT_API_URL", "http://127.0.0.1:8888")


def _hindsight_env_paths() -> list[Path]:
    """Candidate locations for a Hindsight .env file, ordered most-likely-first."""
    home = Path.home()
    cwd = Path.cwd()
    return [
        cwd / ".env",
        cwd / "hindsight-api-slim" / ".env",
        home / ".hindsight" / ".env",
        home / ".config" / "hindsight" / ".env",
    ]


@dataclass
class PostgresMode:
    """Result of `detect_postgres_mode`."""

    mode: str  # "pg0" | "system" | "unknown"
    version: str | None
    lib_dir: str | None  # Postgres pkglibdir (where .dll/.so plugins live)
    share_dir: str | None  # Postgres sharedir/extension (where .control + .sql live)
    data_dir: str | None  # Postgres data directory (postgresql.conf, etc.)
    elevation_required: bool  # True iff writing to lib_dir/share_dir needs admin/root
    notes: str  # Human-readable summary


def _detect_pg0() -> PostgresMode | None:
    """If Hindsight is configured to use pg0 (embedded), return its install layout.

    pg0 lives under ~/.pg0/ and is fully user-writable — no admin needed.
    """
    pg0_root = Path.home() / ".pg0"
    if not pg0_root.exists():
        return None
    install_root = pg0_root / "installation"
    if not install_root.exists():
        return None
    versions = sorted([p.name for p in install_root.iterdir() if p.is_dir()], reverse=True)
    if not versions:
        return None
    version = versions[0]
    pg_root = install_root / version
    # pg0 unpacks pgembed which has standard Postgres layout once extracted
    candidates = [pg_root, pg_root / "pgsql", pg_root / "postgresql", pg_root / "bin" / ".."]
    for c in candidates:
        lib = c / "lib"
        share_ext = c / "share" / "extension"
        if lib.exists() and share_ext.exists():
            return PostgresMode(
                mode="pg0",
                version=version,
                lib_dir=str(lib),
                share_dir=str(share_ext),
                data_dir=str(pg0_root / "instances" / "hindsight" / "data"),
                elevation_required=False,
                notes=f"pg0 embedded Postgres at {pg_root} — user-writable, no admin needed.",
            )
    return PostgresMode(
        mode="pg0",
        version=version,
        lib_dir=None,
        share_dir=None,
        data_dir=str(pg0_root / "instances" / "hindsight" / "data"),
        elevation_required=False,
        notes=(
            f"pg0 directory found at {pg_root} but binaries not yet extracted. "
            "Start Hindsight once with HINDSIGHT_API_DATABASE_URL=pg0 to trigger extraction."
        ),
    )


def _detect_system_postgres() -> PostgresMode | None:
    """If a system Postgres install is available via pg_config, describe it."""
    pg_config = shutil.which("pg_config")
    if not pg_config and _is_windows():
        # Common Windows EnterpriseDB install paths
        for guess in (
            r"C:\Program Files\PostgreSQL\17\bin\pg_config.exe",
            r"C:\Program Files\PostgreSQL\16\bin\pg_config.exe",
            r"C:\Program Files\PostgreSQL\18\bin\pg_config.exe",
            r"C:\Program Files\PostgreSQL\15\bin\pg_config.exe",
        ):
            if Path(guess).exists():
                pg_config = guess
                break
    if not pg_config:
        return None
    try:
        version = subprocess.check_output([pg_config, "--version"], text=True, timeout=5).strip()
        pkglibdir = subprocess.check_output([pg_config, "--pkglibdir"], text=True, timeout=5).strip()
        sharedir = subprocess.check_output([pg_config, "--sharedir"], text=True, timeout=5).strip()
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, FileNotFoundError):
        return None
    share_ext = str(Path(sharedir) / "extension")
    # On Windows, system Postgres lives in Program Files which needs admin.
    # On Linux, /usr/lib/postgresql/* needs sudo. On Mac /usr/local/* may or may not.
    elevation = _is_windows() or pkglibdir.startswith(("/usr/", "/opt/"))
    return PostgresMode(
        mode="system",
        version=version,
        lib_dir=pkglibdir,
        share_dir=share_ext,
        data_dir=None,  # not provided by pg_config; user can pass explicitly
        elevation_required=elevation,
        notes=(
            f"System Postgres at {Path(pg_config).parent}. "
            f"Extension installs require {'admin (UAC)' if _is_windows() else 'sudo'}."
        ),
    )


# ─── Tool: detect_postgres_mode ───────────────────────────────────────────────


@mcp.tool()
def detect_postgres_mode() -> dict[str, Any]:
    """Detect whether Hindsight is configured to use pg0 (user-space, no admin)
    or a system-installed Postgres (requires elevation for extension installs).

    Returns the install layout (lib_dir, share_dir, data_dir) and whether
    elevation is required for extension installs. Tools that install
    extensions use this to auto-route between admin-free and elevated paths.
    """
    pg0 = _detect_pg0()
    sysm = _detect_system_postgres()
    # Prefer pg0 since it's user-writable; fall back to system.
    if pg0 and pg0.lib_dir:
        return asdict(pg0)
    if sysm:
        return asdict(sysm)
    if pg0:
        return asdict(pg0)  # pg0 dir exists but binaries not extracted — still useful info
    return asdict(
        PostgresMode(
            mode="unknown",
            version=None,
            lib_dir=None,
            share_dir=None,
            data_dir=None,
            elevation_required=False,
            notes="No Postgres installation detected (no pg0 dir, no pg_config on PATH).",
        )
    )


# ─── Tool: list_extensions ────────────────────────────────────────────────────


@mcp.tool()
def list_extensions(database_url: str | None = None) -> dict[str, Any]:
    """List all extensions currently installed in the Hindsight database.

    Args:
        database_url: Optional Postgres URL. Defaults to HINDSIGHT_API_DATABASE_URL
            or postgresql://postgres@localhost:5432/hindsight.
    """
    db_url = database_url or os.environ.get(
        "HINDSIGHT_API_DATABASE_URL",
        "postgresql://postgres@localhost:5432/hindsight",
    )
    if db_url.startswith("pg0"):
        return {
            "ok": False,
            "error": "pg0 URLs require running through Hindsight's resolver. Use the system DB URL or "
            "configure HINDSIGHT_API_DATABASE_URL to a resolvable connection string.",
        }
    try:
        import asyncpg  # type: ignore[import-not-found]
    except ImportError:
        return {
            "ok": False,
            "error": "asyncpg not installed. Run: pip install asyncpg",
        }
    import asyncio

    async def _go():
        conn = await asyncpg.connect(db_url)
        try:
            rows = await conn.fetch("SELECT extname AS name, extversion AS version FROM pg_extension ORDER BY extname")
            return [{"name": r["name"], "version": r["version"]} for r in rows]
        finally:
            await conn.close()

    try:
        extensions = asyncio.run(_go())
        return {"ok": True, "extensions": extensions, "count": len(extensions)}
    except Exception as e:
        return {"ok": False, "error": f"Postgres connection failed: {e!r}"}


# ─── Tool: install_extension ──────────────────────────────────────────────────


def _elevate_command() -> list[str] | None:
    """Detect a non-interactive elevation tool. Returns the prefix or None."""
    if _is_windows():
        # Prefer gsudo if available (cached creds, narrowest popup), then sudo (Win 11 24H2+)
        gsudo = shutil.which("gsudo")
        if gsudo:
            return [gsudo]
        sudo = shutil.which("sudo")
        if sudo:
            # Windows native sudo. Default mode prompts UAC; user must have configured "inline".
            return [sudo]
        return None
    # Linux / macOS
    sudo = shutil.which("sudo")
    if sudo:
        return [sudo]
    return None


def _copy_with_elevation(src: Path, dst: Path, *, elevated: bool) -> tuple[bool, str]:
    """Copy src → dst, using elevation when required. Returns (ok, message)."""
    if not elevated:
        try:
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            return True, f"copied {src} → {dst}"
        except PermissionError as e:
            return False, f"permission denied (try elevation): {e!r}"
        except Exception as e:
            return False, f"copy failed: {e!r}"
    elev = _elevate_command()
    if not elev:
        return False, (
            "Elevation required but no elevation tool found. Install gsudo (https://gerardog.github.io/gsudo/) "
            "or enable Windows 11 24H2 sudo in Settings → System → For developers."
        )
    if _is_windows():
        cmd = elev + ["cmd", "/c", "copy", "/Y", str(src), str(dst)]
    else:
        cmd = elev + ["cp", "-f", str(src), str(dst)]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        if result.returncode == 0:
            return True, f"elevated copy {src} → {dst}"
        return False, f"elevated copy failed (rc={result.returncode}): {result.stderr or result.stdout}"
    except subprocess.TimeoutExpired:
        return False, "elevation prompt timed out (no response after 60s)"


@mcp.tool()
def install_extension(
    name: str,
    dll_or_so_path: str,
    sql_file_path: str,
    control_file_path: str,
    extra_sql_files: list[str] | None = None,
) -> dict[str, Any]:
    """Install a built Postgres extension into the active Postgres install.

    Auto-detects pg0 (user-space, no admin) vs system Postgres (requires elevation).
    For system Postgres, attempts to use gsudo (Windows) or sudo (Linux/Mac) for the copy.

    Args:
        name: Extension name (e.g., 'vchord').
        dll_or_so_path: Path to the built .dll (Windows) or .so (Linux/Mac) file.
        sql_file_path: Path to the extension's main SQL file (e.g., 'vchord--0.0.0.sql').
        control_file_path: Path to the extension's .control file.
        extra_sql_files: Optional list of additional SQL files (upgrade scripts, etc.).
    """
    src_dll = Path(dll_or_so_path)
    src_sql = Path(sql_file_path)
    src_control = Path(control_file_path)
    for label, p in [("dll/so", src_dll), ("sql", src_sql), ("control", src_control)]:
        if not p.exists():
            return {"ok": False, "error": f"{label} not found at {p}"}

    mode_dict = detect_postgres_mode()
    if mode_dict["mode"] == "unknown" or not mode_dict.get("lib_dir"):
        return {"ok": False, "error": "Could not detect Postgres install layout.", "detected": mode_dict}

    lib_dir = Path(mode_dict["lib_dir"])
    share_dir = Path(mode_dict["share_dir"])
    elevated = bool(mode_dict["elevation_required"])

    actions: list[dict[str, Any]] = []

    # Copy extension binary
    dst_dll = lib_dir / src_dll.name
    ok, msg = _copy_with_elevation(src_dll, dst_dll, elevated=elevated)
    actions.append({"step": "copy-binary", "ok": ok, "message": msg, "path": str(dst_dll)})
    if not ok:
        return {"ok": False, "actions": actions, "mode": mode_dict["mode"]}

    # Copy SQL file
    dst_sql = share_dir / src_sql.name
    ok, msg = _copy_with_elevation(src_sql, dst_sql, elevated=elevated)
    actions.append({"step": "copy-sql", "ok": ok, "message": msg, "path": str(dst_sql)})
    if not ok:
        return {"ok": False, "actions": actions, "mode": mode_dict["mode"]}

    # Copy control file
    dst_control = share_dir / src_control.name
    ok, msg = _copy_with_elevation(src_control, dst_control, elevated=elevated)
    actions.append({"step": "copy-control", "ok": ok, "message": msg, "path": str(dst_control)})
    if not ok:
        return {"ok": False, "actions": actions, "mode": mode_dict["mode"]}

    # Copy any extra SQL files (upgrade scripts)
    for extra in extra_sql_files or []:
        src_extra = Path(extra)
        if not src_extra.exists():
            actions.append({"step": "copy-extra", "ok": False, "message": f"not found: {extra}"})
            continue
        dst_extra = share_dir / src_extra.name
        ok, msg = _copy_with_elevation(src_extra, dst_extra, elevated=elevated)
        actions.append({"step": "copy-extra", "ok": ok, "message": msg, "path": str(dst_extra)})

    return {
        "ok": True,
        "name": name,
        "mode": mode_dict["mode"],
        "elevation_used": elevated,
        "actions": actions,
        "next_steps": [
            f"In psql: CREATE EXTENSION {name} CASCADE;",
            "If extension is in shared_preload_libraries list, restart Postgres first.",
        ],
    }


# ─── Tool: uninstall_extension ────────────────────────────────────────────────


@mcp.tool()
def uninstall_extension(name: str) -> dict[str, Any]:
    """Remove an extension's files from Postgres lib + share dirs.

    Does NOT execute `DROP EXTENSION` in the database — the user should do that first
    in psql to avoid leaving dangling references.
    """
    mode_dict = detect_postgres_mode()
    if not mode_dict.get("lib_dir"):
        return {"ok": False, "error": "Could not detect Postgres install layout."}
    lib_dir = Path(mode_dict["lib_dir"])
    share_dir = Path(mode_dict["share_dir"])
    elevated = bool(mode_dict["elevation_required"])

    targets: list[Path] = []
    for ext in (".dll", ".so", ".dylib"):
        candidate = lib_dir / f"{name}{ext}"
        if candidate.exists():
            targets.append(candidate)
    for fname in (f"{name}.control",):
        candidate = share_dir / fname
        if candidate.exists():
            targets.append(candidate)
    targets.extend(share_dir.glob(f"{name}--*.sql"))

    actions = []
    for t in targets:
        try:
            if elevated:
                elev = _elevate_command()
                if not elev:
                    actions.append({"path": str(t), "ok": False, "message": "elevation tool missing"})
                    continue
                cmd = elev + (["cmd", "/c", "del", "/F", "/Q", str(t)] if _is_windows() else ["rm", "-f", str(t)])
                rc = subprocess.run(cmd, capture_output=True, text=True, timeout=30).returncode
                actions.append({"path": str(t), "ok": rc == 0, "message": f"elevated rm rc={rc}"})
            else:
                t.unlink()
                actions.append({"path": str(t), "ok": True, "message": "removed"})
        except Exception as e:
            actions.append({"path": str(t), "ok": False, "message": repr(e)})

    return {
        "ok": all(a["ok"] for a in actions),
        "name": name,
        "mode": mode_dict["mode"],
        "removed_count": sum(1 for a in actions if a["ok"]),
        "actions": actions,
        "next_steps": [
            f"If {name} was loaded, restart Postgres before reinstalling.",
            f"In psql: DROP EXTENSION IF EXISTS {name};  (do this BEFORE removing files in the future)",
        ],
    }


# ─── Tool: set_hindsight_env / get_hindsight_env ──────────────────────────────


@mcp.tool()
def get_hindsight_env() -> dict[str, Any]:
    """Read the current Hindsight environment configuration.

    Returns env-var values from the first found .env file plus relevant
    process-environment variables.
    """
    relevant_keys = [
        "HINDSIGHT_API_DATABASE_URL",
        "HINDSIGHT_API_VECTOR_EXTENSION",
        "HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL",
        "HINDSIGHT_API_EMBEDDINGS_LOCAL_TRUST_REMOTE_CODE",
        "HINDSIGHT_API_RERANKER_PROVIDER",
        "HINDSIGHT_API_RERANKER_LOCAL_MODEL",
        "HINDSIGHT_API_RERANKER_LOCAL_TRUST_REMOTE_CODE",
        "HINDSIGHT_API_RUN_MIGRATIONS_ON_STARTUP",
    ]
    file_env: dict[str, str] = {}
    file_used: str | None = None
    for env_path in _hindsight_env_paths():
        if env_path.exists():
            for line in env_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                file_env[k.strip()] = v.strip().strip('"').strip("'")
            file_used = str(env_path)
            break
    process_env = {k: os.environ[k] for k in relevant_keys if k in os.environ}
    merged = {k: file_env.get(k) or process_env.get(k) for k in relevant_keys}
    return {
        "ok": True,
        "env_file": file_used,
        "from_file": {k: file_env.get(k) for k in relevant_keys if file_env.get(k)},
        "from_process": process_env,
        "effective": {k: v for k, v in merged.items() if v is not None},
    }


@mcp.tool()
def set_hindsight_env(key: str, value: str, env_file: str | None = None) -> dict[str, Any]:
    """Set or update a Hindsight env-var entry in a .env file.

    Args:
        key: Env-var name (e.g., 'HINDSIGHT_API_VECTOR_EXTENSION').
        value: New value (e.g., 'vchord').
        env_file: Optional explicit path to a .env file. If omitted, the first
            existing candidate from get_hindsight_env() is used; if none exist,
            ~/.hindsight/.env is created.

    Note: Hindsight reads env vars at process start. After setting, Hindsight
    must be restarted for changes to take effect.
    """
    target: Path | None = None
    if env_file:
        target = Path(env_file)
    else:
        for candidate in _hindsight_env_paths():
            if candidate.exists():
                target = candidate
                break
    if target is None:
        target = Path.home() / ".hindsight" / ".env"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("", encoding="utf-8")

    lines = target.read_text(encoding="utf-8").splitlines() if target.exists() else []
    new_lines: list[str] = []
    found = False
    for line in lines:
        stripped = line.strip()
        if stripped.startswith(f"{key}=") or stripped.startswith(f"#{key}="):
            new_lines.append(f"{key}={value}")
            found = True
        else:
            new_lines.append(line)
    if not found:
        new_lines.append(f"{key}={value}")
    target.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    return {
        "ok": True,
        "env_file": str(target),
        "key": key,
        "value": value,
        "added": not found,
        "next_steps": ["Restart Hindsight (`hindsight_status` → kill → start) for the env change to take effect."],
    }


# ─── Tool: hindsight_status ───────────────────────────────────────────────────


@mcp.tool()
def hindsight_status() -> dict[str, Any]:
    """Report whether the Hindsight HTTP API is reachable and what the embedding state looks like."""
    url = _hindsight_url()
    status: dict[str, Any] = {"url": url}
    try:
        r = httpx.get(f"{url}/health", timeout=3)
        status["api_reachable"] = r.status_code == 200
        status["health"] = r.json() if r.status_code == 200 else None
    except httpx.HTTPError as e:
        status["api_reachable"] = False
        status["error"] = repr(e)

    db_url = os.environ.get(
        "HINDSIGHT_API_DATABASE_URL",
        "postgresql://postgres@localhost:5432/hindsight",
    )
    if db_url.startswith("pg0"):
        status["embedding_state"] = "pg0 mode — query through Hindsight API"
        return status
    try:
        import asyncio

        import asyncpg  # type: ignore[import-not-found]

        async def _go():
            conn = await asyncpg.connect(db_url)
            try:
                col = await conn.fetchrow(
                    """
                    SELECT format_type(a.atttypid, a.atttypmod) AS t
                    FROM pg_attribute a JOIN pg_class c ON a.attrelid=c.oid
                    WHERE a.attname='embedding' AND c.relname='memory_units'
                    """
                )
                total = await conn.fetchval("SELECT COUNT(*) FROM memory_units")
                with_emb = await conn.fetchval("SELECT COUNT(*) FROM memory_units WHERE embedding IS NOT NULL")
                return {"column_type": col["t"] if col else None, "total": total, "embedded": with_emb}

            finally:
                await conn.close()

        status["embedding_state"] = asyncio.run(_go())
    except Exception as e:
        status["embedding_state"] = {"error": repr(e)}

    return status


# ─── Tool: migrate_embeddings ─────────────────────────────────────────────────


@mcp.tool()
def migrate_embeddings(
    schema: str = "public",
    bank: str | None = None,
    batch_size: int = 16,
    auto_backup_path: str | None = None,
    verify_recall: bool = True,
    skip_index_rebuild: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run `hindsight-admin reindex-embeddings` to re-encode any rows where embedding IS NULL.

    Used after swapping the embedding model + ALTER'ing the column type. Idempotent.

    Args:
        schema: Database schema (default 'public').
        bank: Optional bank id filter.
        batch_size: Rows per encode/update batch (tune to GPU VRAM).
        auto_backup_path: Optional path to a .zip file; runs a backup before re-embedding.
        verify_recall: If True, runs a self-match sanity check after re-embedding.
        skip_index_rebuild: If True, skips REINDEX of HNSW/IVFFlat/vchordrq indexes.
        dry_run: If True, only counts pending rows without encoding.
    """
    cmd: list[str] = []
    hindsight_admin = shutil.which("hindsight-admin")
    if hindsight_admin:
        cmd = [hindsight_admin, "reindex-embeddings"]
    else:
        cmd = [sys.executable, "-m", "hindsight_api.admin.cli", "reindex-embeddings"]
    cmd += ["--schema", schema, "--batch-size", str(batch_size), "--yes"]
    if bank:
        cmd += ["--bank", bank]
    if auto_backup_path:
        cmd += ["--auto-backup", auto_backup_path]
    if verify_recall:
        cmd += ["--verify-recall"]
    if skip_index_rebuild:
        cmd += ["--skip-index-rebuild"]
    if dry_run:
        cmd += ["--dry-run"]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
        return {
            "ok": result.returncode == 0,
            "command": " ".join(cmd),
            "stdout": result.stdout[-4000:] if result.stdout else "",
            "stderr": result.stderr[-2000:] if result.stderr else "",
            "returncode": result.returncode,
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "error": "Migration exceeded 1-hour timeout. Check progress with hindsight_status."}
    except FileNotFoundError:
        return {
            "ok": False,
            "error": "hindsight-admin entry point not found. Ensure hindsight-api-slim is installed in the active environment.",
        }


# ─── Tool: verify_recall ──────────────────────────────────────────────────────


@mcp.tool()
def verify_recall(schema: str = "public", sample_size: int = 5) -> dict[str, Any]:
    """Run a recall self-match sanity check via the reindex-embeddings command's verify-recall path.

    This is the same check `migrate_embeddings(verify_recall=True)` runs internally —
    exposed separately so you can validate a healthy embedding pipeline without
    needing to migrate anything.
    """
    return migrate_embeddings(
        schema=schema,
        verify_recall=True,
        skip_index_rebuild=True,
        # No backup, no batch encoding — recall check runs even with 0 pending rows.
    )


# ─── Tool: upgrade_embedding_pipeline ─────────────────────────────────────────


@mcp.tool()
def upgrade_embedding_pipeline(
    target_model: str,
    target_dimension: int,
    backup_path: str,
    *,
    trust_remote_code: bool = True,
    use_halfvec: bool = False,
    use_vchord: bool = False,
    schema: str = "public",
) -> dict[str, Any]:
    """Orchestrator for the full Qwen3-style embedding upgrade.

    Workflow:
      1. Create a backup at backup_path.
      2. Set HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL / TRUST_REMOTE_CODE in .env.
      3. (If use_vchord) Set HINDSIGHT_API_VECTOR_EXTENSION=vchord.
      4. Print the ALTER COLUMN SQL the user must run (manual step — destructive).
      5. After the user runs ALTER, call migrate_embeddings.
      6. Verify recall.

    NOTE: Step 4 is intentionally NOT auto-executed — wiping embeddings is destructive
    and should be a deliberate action with the user's eyes on the SQL.
    """
    actions: list[dict[str, Any]] = []

    # Step 1: backup (lightweight — relies on hindsight-admin backup)
    backup_cmd = ["hindsight-admin", "backup", backup_path]
    if shutil.which("hindsight-admin") is None:
        backup_cmd = [sys.executable, "-m", "hindsight_api.admin.cli", "backup", backup_path]
    try:
        r = subprocess.run(backup_cmd, capture_output=True, text=True, timeout=600)
        actions.append({"step": "backup", "ok": r.returncode == 0, "stdout": r.stdout[-500:]})
        if r.returncode != 0:
            return {"ok": False, "actions": actions, "abort_reason": "backup failed; aborting before destructive steps"}
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        actions.append({"step": "backup", "ok": False, "error": repr(e)})
        return {"ok": False, "actions": actions, "abort_reason": "backup tool unavailable"}

    # Step 2: set env vars
    set_hindsight_env("HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL", target_model)
    set_hindsight_env("HINDSIGHT_API_EMBEDDINGS_LOCAL_TRUST_REMOTE_CODE", str(trust_remote_code).lower())
    actions.append({"step": "set-model-env", "ok": True, "model": target_model})

    if use_vchord:
        set_hindsight_env("HINDSIGHT_API_VECTOR_EXTENSION", "vchord")
        actions.append({"step": "set-vector-extension", "ok": True, "extension": "vchord"})

    # Step 3: emit ALTER guidance — user runs this manually
    pg_type = "halfvec" if use_halfvec else "vector"
    alter_sql = (
        f"-- Run these in psql AFTER stopping Hindsight, BEFORE running migrate_embeddings:\n"
        f"ALTER TABLE {schema}.memory_units  ALTER COLUMN embedding TYPE {pg_type}({target_dimension}) USING NULL;\n"
        f"ALTER TABLE {schema}.mental_models ALTER COLUMN embedding TYPE {pg_type}({target_dimension}) USING NULL;\n"
    )
    actions.append({"step": "alter-sql-required", "ok": True, "sql": alter_sql})

    return {
        "ok": True,
        "stage": "ready-for-alter",
        "actions": actions,
        "next_steps": [
            "1. Stop Hindsight.",
            f"2. Run the SQL in actions[-1].sql via psql against schema '{schema}'.",
            "3. Restart Hindsight (it will load the new embedding model).",
            "4. Call `migrate_embeddings(schema=..., auto_backup_path=..., verify_recall=true)` to re-encode.",
        ],
    }


# ─── Tool: check_hindsight_update ─────────────────────────────────────────────


COMPAT_MANIFEST_URL = "https://raw.githubusercontent.com/grimmjoww/hindsight-installer-mcp/main/compat.json"
PYPI_HINDSIGHT_API = "https://pypi.org/pypi/hindsight-api/json"


def _installed_hindsight_version() -> str | None:
    """Best-effort detection of the locally installed Hindsight version."""
    try:
        from importlib.metadata import PackageNotFoundError, version

        try:
            return version("hindsight-api")
        except PackageNotFoundError:
            pass
        try:
            return version("hindsight-api-slim")
        except PackageNotFoundError:
            pass
    except Exception:
        pass
    # Fallback: ask hindsight-admin
    admin = shutil.which("hindsight-admin")
    if admin:
        try:
            out = subprocess.check_output([admin, "--version"], text=True, timeout=5).strip()
            for tok in out.split():
                if tok and tok[0].isdigit():
                    return tok
        except Exception:
            pass
    return None


def _semver_tuple(v: str) -> tuple[int, ...]:
    """Crude semver parse: '0.5.4' → (0, 5, 4). Pre-release suffixes truncated."""
    main = v.split("-")[0].split("+")[0]
    parts = []
    for p in main.split("."):
        try:
            parts.append(int(p))
        except ValueError:
            break
    return tuple(parts) or (0,)


def _matches_pattern(version: str, pattern: str) -> bool:
    """Simple version-pattern matcher: '0.5.x', '<0.5.0', '>=0.5.4'."""
    pattern = pattern.strip()
    v_tuple = _semver_tuple(version)
    if pattern.endswith(".x"):
        prefix = _semver_tuple(pattern[:-2])
        return v_tuple[: len(prefix)] == prefix
    for op in ("<=", ">=", "<", ">", "=="):
        if pattern.startswith(op):
            target = _semver_tuple(pattern[len(op) :].strip())
            if op == "<":
                return v_tuple < target
            if op == "<=":
                return v_tuple <= target
            if op == ">":
                return v_tuple > target
            if op == ">=":
                return v_tuple >= target
            if op == "==":
                return v_tuple == target
    return v_tuple == _semver_tuple(pattern)


@mcp.tool()
def check_hindsight_update() -> dict[str, Any]:
    """Check whether a newer Hindsight release is available and whether it's
    flagged compatible with this MCP version per the published compat matrix.

    Returns:
        {
            installed: <current local version or null>,
            latest: <latest version on PyPI>,
            update_available: bool,
            compatibility: { status, severity, message, advisories[] },
            recommendation: human-readable
        }
    """
    installed = _installed_hindsight_version()

    latest: str | None = None
    try:
        r = httpx.get(PYPI_HINDSIGHT_API, timeout=8)
        if r.status_code == 200:
            data = r.json()
            latest = data.get("info", {}).get("version")
    except httpx.HTTPError as e:
        return {
            "ok": False,
            "error": f"Could not reach PyPI to check latest version: {e!r}",
            "installed": installed,
        }

    manifest: dict[str, Any] = {}
    try:
        r = httpx.get(COMPAT_MANIFEST_URL, timeout=8)
        if r.status_code == 200:
            manifest = r.json()
    except httpx.HTTPError:
        manifest = {}

    update_available = False
    if installed and latest:
        update_available = _semver_tuple(latest) > _semver_tuple(installed)

    advisories: list[dict[str, Any]] = []
    severity = "ok"
    message = "No advisories. Update is safe per current compat matrix."

    target = latest or installed
    if target and manifest:
        min_supported = manifest.get("minimum_supported_hindsight")
        if min_supported and _semver_tuple(target) < _semver_tuple(min_supported):
            severity = "block"
            message = f"Hindsight {target} is below minimum_supported {min_supported}."
        max_safe = manifest.get("maximum_known_safe_hindsight")
        if max_safe and _semver_tuple(target) > _semver_tuple(max_safe):
            severity = "warn"
            message = (
                f"Hindsight {target} is newer than maximum_known_safe {max_safe}. "
                "Untested with this MCP version. Run validate_compatibility before upgrading."
            )
        for adv in manifest.get("known_advisories", []):
            pat = adv.get("applies_to", "")
            if pat and _matches_pattern(target, pat):
                advisories.append(adv)
                if adv.get("severity") == "block":
                    severity = "block"
                    message = adv.get("message", message)

    rec_parts = []
    if not installed:
        rec_parts.append("Hindsight does not appear to be installed in this environment.")
    elif update_available and severity == "block":
        rec_parts.append(f"DO NOT update yet — {message}")
    elif update_available and severity == "warn":
        rec_parts.append(f"Update available ({installed} → {latest}) but proceed with caution: {message}")
        rec_parts.append("Recommended: run `validate_compatibility` then `safe_upgrade_hindsight`.")
    elif update_available:
        rec_parts.append(
            f"Update available ({installed} → {latest}). Use `safe_upgrade_hindsight` to back up + verify."
        )
    else:
        rec_parts.append(f"You are on the latest version ({installed}).")

    return {
        "ok": True,
        "installed": installed,
        "latest": latest,
        "update_available": update_available,
        "compatibility": {
            "status": severity,
            "message": message,
            "advisories": advisories,
            "tested_against": manifest.get("tested_against", []),
            "minimum_supported": manifest.get("minimum_supported_hindsight"),
            "maximum_known_safe": manifest.get("maximum_known_safe_hindsight"),
        },
        "recommendation": " ".join(rec_parts),
    }


# ─── Tool: validate_compatibility ─────────────────────────────────────────────


@mcp.tool()
def validate_compatibility(target_version: str | None = None) -> dict[str, Any]:
    """Pre-flight compatibility check before applying a Hindsight update.

    Runs three probes:
      1. CLI surface — does `hindsight-admin reindex-embeddings --help` still expose
         the flags this MCP relies on (--auto-backup, --verify-recall, --bank, etc.)
      2. Embedding column type discovery — does `reindex-embeddings --dry-run` succeed
      3. API health — is Hindsight currently responsive

    Args:
        target_version: Optional. If passed, also looks up that version's status in
            the published compat manifest.
    """
    probes: list[dict[str, Any]] = []
    required_flags = [
        "--auto-backup",
        "--verify-recall",
        "--bank",
        "--schema",
        "--batch-size",
        "--skip-index-rebuild",
        "--dry-run",
        "--yes",
    ]

    # Probe 1: CLI surface
    cmd_base: list[str]
    admin = shutil.which("hindsight-admin")
    if admin:
        cmd_base = [admin]
    else:
        cmd_base = [sys.executable, "-m", "hindsight_api.admin.cli"]
    try:
        r = subprocess.run(cmd_base + ["reindex-embeddings", "--help"], capture_output=True, text=True, timeout=15)
        help_text = r.stdout + r.stderr
        missing = [f for f in required_flags if f not in help_text]
        probes.append(
            {
                "name": "cli-surface",
                "ok": len(missing) == 0,
                "required_flags_present": [f for f in required_flags if f not in missing],
                "missing_flags": missing,
            }
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        probes.append({"name": "cli-surface", "ok": False, "error": repr(e)})

    # Probe 2: dry-run reindex
    try:
        r = subprocess.run(
            cmd_base + ["reindex-embeddings", "--dry-run", "--yes"], capture_output=True, text=True, timeout=60
        )
        probes.append(
            {
                "name": "dry-run-reindex",
                "ok": r.returncode == 0,
                "stdout_tail": (r.stdout or "")[-1000:],
                "returncode": r.returncode,
            }
        )
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        probes.append({"name": "dry-run-reindex", "ok": False, "error": repr(e)})

    # Probe 3: API health
    try:
        r = httpx.get(f"{_hindsight_url()}/health", timeout=3)
        probes.append({"name": "api-health", "ok": r.status_code == 200, "status_code": r.status_code})
    except httpx.HTTPError as e:
        probes.append({"name": "api-health", "ok": False, "error": repr(e)})

    # Manifest lookup if target_version specified
    manifest_status: dict[str, Any] = {}
    if target_version:
        try:
            r = httpx.get(COMPAT_MANIFEST_URL, timeout=8)
            manifest = r.json() if r.status_code == 200 else {}
            tested = [t for t in manifest.get("tested_against", []) if t.get("hindsight_version") == target_version]
            manifest_status = {
                "found_in_tested_list": bool(tested),
                "tested_entry": tested[0] if tested else None,
            }
        except httpx.HTTPError as e:
            manifest_status = {"error": repr(e)}

    all_ok = all(p["ok"] for p in probes)
    return {
        "ok": all_ok,
        "verdict": "compatible" if all_ok else "incompatible-or-degraded",
        "probes": probes,
        "manifest_status": manifest_status,
        "recommendation": (
            "All probes green. Safe to upgrade with `safe_upgrade_hindsight`."
            if all_ok
            else "One or more probes failed. Investigate before upgrading; running safe_upgrade_hindsight may break."
        ),
    }


# ─── Tool: safe_upgrade_hindsight ─────────────────────────────────────────────


@mcp.tool()
def safe_upgrade_hindsight(
    target_version: str | None = None,
    backup_path: str = "./pre-upgrade-backup.zip",
    skip_validation: bool = False,
    rollback_on_failure: bool = True,
) -> dict[str, Any]:
    """Coordinated, reversible Hindsight upgrade with pre-flight + post-flight verification.

    Workflow:
      1. (Optional) validate_compatibility — abort if probes fail.
      2. hindsight-admin backup → backup_path.
      3. Record currently-installed Hindsight version.
      4. pip install -U hindsight-api (or to a specific target_version if passed).
      5. validate_compatibility again on the upgraded version.
      6. If post-upgrade validation fails AND rollback_on_failure=True:
         pip install hindsight-api==<previous_version> to roll back.
         The user still has the backup_path zip if data was touched.

    Args:
        target_version: Optional pinned version (e.g., '0.5.5'). Defaults to latest.
        backup_path: Where to write the pre-upgrade backup zip.
        skip_validation: Skip the pre-flight validate_compatibility probes (not recommended).
        rollback_on_failure: If post-upgrade validation fails, attempt pip rollback to previous.
    """
    actions: list[dict[str, Any]] = []
    previous_version = _installed_hindsight_version()
    actions.append({"step": "record-current-version", "version": previous_version})

    # Step 1: pre-flight validation
    if not skip_validation:
        pre = validate_compatibility()
        actions.append({"step": "pre-flight-validate", "ok": pre["ok"], "verdict": pre["verdict"]})
        if not pre["ok"]:
            return {
                "ok": False,
                "stage": "aborted-pre-flight",
                "actions": actions,
                "abort_reason": "Pre-flight validation failed; refusing to upgrade. "
                "Pass skip_validation=true to override (not recommended).",
                "pre_flight": pre,
            }

    # Step 2: backup
    backup_cmd: list[str]
    admin = shutil.which("hindsight-admin")
    if admin:
        backup_cmd = [admin, "backup", backup_path]
    else:
        backup_cmd = [sys.executable, "-m", "hindsight_api.admin.cli", "backup", backup_path]
    try:
        r = subprocess.run(backup_cmd, capture_output=True, text=True, timeout=600)
        actions.append({"step": "backup", "ok": r.returncode == 0, "path": backup_path})
        if r.returncode != 0:
            return {"ok": False, "stage": "backup-failed", "actions": actions, "stderr": r.stderr[-500:]}
    except (subprocess.TimeoutExpired, FileNotFoundError) as e:
        actions.append({"step": "backup", "ok": False, "error": repr(e)})
        return {"ok": False, "stage": "backup-failed", "actions": actions}

    # Step 3: pip upgrade
    pkg_spec = f"hindsight-api=={target_version}" if target_version else "hindsight-api"
    upgrade_flag = [] if target_version else ["-U"]
    try:
        r = subprocess.run(
            [sys.executable, "-m", "pip", "install", *upgrade_flag, pkg_spec],
            capture_output=True,
            text=True,
            timeout=600,
        )
        actions.append({"step": "pip-upgrade", "ok": r.returncode == 0, "stdout_tail": (r.stdout or "")[-500:]})
        if r.returncode != 0:
            return {
                "ok": False,
                "stage": "pip-upgrade-failed",
                "actions": actions,
                "stderr": (r.stderr or "")[-500:],
            }
    except subprocess.TimeoutExpired:
        actions.append({"step": "pip-upgrade", "ok": False, "error": "timeout"})
        return {"ok": False, "stage": "pip-upgrade-failed", "actions": actions}

    new_version = _installed_hindsight_version()
    actions.append({"step": "record-new-version", "version": new_version})

    # Step 4: post-flight validation
    post = validate_compatibility()
    actions.append({"step": "post-flight-validate", "ok": post["ok"], "verdict": post["verdict"]})

    if post["ok"]:
        return {
            "ok": True,
            "stage": "upgraded-and-verified",
            "previous_version": previous_version,
            "new_version": new_version,
            "backup_path": backup_path,
            "actions": actions,
        }

    # Step 5: rollback
    if rollback_on_failure and previous_version:
        rollback = subprocess.run(
            [sys.executable, "-m", "pip", "install", f"hindsight-api=={previous_version}"],
            capture_output=True,
            text=True,
            timeout=600,
        )
        actions.append({"step": "rollback", "ok": rollback.returncode == 0, "to_version": previous_version})
        return {
            "ok": False,
            "stage": "rolled-back-after-post-flight-failure",
            "previous_version": previous_version,
            "attempted_version": new_version,
            "backup_path": backup_path,
            "actions": actions,
            "post_flight": post,
        }

    return {
        "ok": False,
        "stage": "upgraded-but-post-flight-failed",
        "previous_version": previous_version,
        "new_version": new_version,
        "backup_path": backup_path,
        "actions": actions,
        "post_flight": post,
        "warning": "Hindsight is upgraded but post-flight validation failed. "
        "Either restore from backup or pip install the previous version manually.",
    }


def main() -> None:
    """Stdio MCP server entrypoint."""
    mcp.run()


if __name__ == "__main__":
    main()
