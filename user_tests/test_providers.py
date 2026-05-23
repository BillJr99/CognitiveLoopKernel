"""Smoke tests for the seven shipped providers.

The harness must register all seven providers (shell, claude, codex,
gemini, pi, ollama, openwebui), but only the ``shell`` provider is
required to actually work without external dependencies.  These tests
verify both invariants.
"""

from __future__ import annotations

import json
from pathlib import Path

from .conftest import run_clk


SHIPPED_PROVIDERS = ("shell", "claude", "codex", "gemini", "pi", "ollama", "openwebui")


def test_all_providers_registered(initialized_project: Path) -> None:
    providers = json.loads(
        (initialized_project / ".clk" / "config" / "providers.json").read_text()
    )
    for name in SHIPPED_PROVIDERS:
        assert name in providers["providers"], (
            f"provider {name!r} not in providers.json"
        )


def test_shell_provider_always_available(initialized_project: Path) -> None:
    res = run_clk("providers", cwd=initialized_project)
    assert res.returncode == 0, res.stderr
    data = json.loads(res.stdout)
    assert data["available"]["shell"] is True


def test_default_active_provider_is_shell(initialized_project: Path) -> None:
    res = run_clk("providers", cwd=initialized_project)
    assert res.returncode == 0
    data = json.loads(res.stdout)
    assert data["active"] == "shell"


def test_switching_active_provider_persists(initialized_project: Path) -> None:
    """Update the active provider via the providers.json file and ensure
    the CLI reflects the change."""
    prov_path = initialized_project / ".clk" / "config" / "providers.json"
    data = json.loads(prov_path.read_text())
    data["active"] = "ollama"          # local-only provider, no network dep
    prov_path.write_text(json.dumps(data, indent=2))

    res = run_clk("providers", cwd=initialized_project)
    assert res.returncode == 0
    parsed = json.loads(res.stdout)
    assert parsed["active"] == "ollama"


def test_each_provider_reports_availability(initialized_project: Path) -> None:
    """Every shipped provider must appear in the availability map (as bool)."""
    res = run_clk("providers", cwd=initialized_project)
    assert res.returncode == 0
    data = json.loads(res.stdout)
    for name in SHIPPED_PROVIDERS:
        assert name in data["available"], f"{name!r} not in availability map"
        assert isinstance(data["available"][name], bool)
