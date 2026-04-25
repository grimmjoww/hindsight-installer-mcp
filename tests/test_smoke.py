"""Smoke tests for hindsight-installer-mcp.

These don't exercise full MCP roundtrips; they validate the helper functions
load and tool functions return well-formed dicts on a system where Hindsight
may or may not be present.
"""

from __future__ import annotations

import importlib

import pytest


def test_module_imports():
    mod = importlib.import_module("hindsight_installer_mcp.server")
    assert mod is not None
    assert hasattr(mod, "mcp")
    assert hasattr(mod, "main")


def test_detect_postgres_mode_returns_dict():
    from hindsight_installer_mcp.server import detect_postgres_mode

    result = detect_postgres_mode()
    assert isinstance(result, dict)
    assert "mode" in result
    assert result["mode"] in {"pg0", "system", "unknown"}
    assert "elevation_required" in result
    assert isinstance(result["elevation_required"], bool)


def test_get_hindsight_env_returns_structured_dict():
    from hindsight_installer_mcp.server import get_hindsight_env

    result = get_hindsight_env()
    assert result["ok"] is True
    assert "from_file" in result
    assert "from_process" in result
    assert "effective" in result


def test_install_extension_rejects_missing_files():
    from hindsight_installer_mcp.server import install_extension

    result = install_extension(
        name="bogus",
        dll_or_so_path="/nonexistent/path/bogus.dll",
        sql_file_path="/nonexistent/path/bogus--0.1.sql",
        control_file_path="/nonexistent/path/bogus.control",
    )
    assert result["ok"] is False
    assert "not found" in result["error"]


def test_set_hindsight_env_writes_to_explicit_path(tmp_path):
    from hindsight_installer_mcp.server import set_hindsight_env

    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING=value\n", encoding="utf-8")

    result = set_hindsight_env(
        key="HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL",
        value="Qwen/Qwen3-Embedding-4B",
        env_file=str(env_file),
    )
    assert result["ok"] is True
    contents = env_file.read_text(encoding="utf-8")
    assert "EXISTING=value" in contents
    assert "HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL=Qwen/Qwen3-Embedding-4B" in contents


def test_set_hindsight_env_replaces_existing_key(tmp_path):
    from hindsight_installer_mcp.server import set_hindsight_env

    env_file = tmp_path / ".env"
    env_file.write_text(
        "HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL=BAAI/bge-small-en-v1.5\nOTHER=keep\n",
        encoding="utf-8",
    )

    result = set_hindsight_env(
        key="HINDSIGHT_API_EMBEDDINGS_LOCAL_MODEL",
        value="Qwen/Qwen3-Embedding-4B",
        env_file=str(env_file),
    )
    assert result["ok"] is True
    contents = env_file.read_text(encoding="utf-8")
    assert "BAAI/bge-small-en-v1.5" not in contents
    assert "Qwen/Qwen3-Embedding-4B" in contents
    assert "OTHER=keep" in contents


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
