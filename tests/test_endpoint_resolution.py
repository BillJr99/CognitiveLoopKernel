"""Endpoint resolution: the probe and the runtime must agree on *where*
an HTTP provider was actually found.

Regression tests for a guided-mode failure: discovery "found" Ollama via
list_models' silent internal docker-host fallback but reported the dead
localhost endpoint, which the wizard persisted (including the
CLK_OLLAMA_ENDPOINT env override) — and at runtime the env var outranked
the provider's own rescue, so every invoke() hit connection-refused.
"""

from __future__ import annotations

from clk_harness import webui_router
from clk_harness.providers import _endpoint_fallback, ollama, openwebui

LOCAL = "http://localhost:11434"
SWAP = "http://host.docker.internal:11434"


# ---------------------------------------------------------------------------
# _probe_blocking: report the candidate endpoint that actually answered
# ---------------------------------------------------------------------------

def test_probe_reports_swap_endpoint_when_localhost_dead(monkeypatch) -> None:
    monkeypatch.setattr(
        _endpoint_fallback, "probe_endpoint", lambda ep, **kw: ep == SWAP
    )
    monkeypatch.setattr(
        ollama, "list_models", lambda ep, **kw: ["llama3.1"] if ep == SWAP else []
    )
    res = webui_router._probe_blocking("ollama", LOCAL, "")
    assert res["reachable"] is True
    assert res["models"] == ["llama3.1"]
    assert res["endpoint"] == SWAP


def test_probe_keeps_localhost_when_it_answers(monkeypatch) -> None:
    monkeypatch.setattr(
        _endpoint_fallback, "probe_endpoint", lambda ep, **kw: ep == LOCAL
    )
    monkeypatch.setattr(
        ollama, "list_models", lambda ep, **kw: ["llama3.1"] if ep == LOCAL else []
    )
    res = webui_router._probe_blocking("ollama", LOCAL, "")
    assert res["endpoint"] == LOCAL
    assert res["models"] == ["llama3.1"]


def test_probe_reachable_swap_without_models(monkeypatch) -> None:
    # E.g. OpenWebUI reachable only at the docker host but refusing the
    # model list until a key is supplied: still report the working URL.
    swap8080 = "http://host.docker.internal:8080"
    monkeypatch.setattr(
        _endpoint_fallback, "probe_endpoint", lambda ep, **kw: ep == swap8080
    )
    monkeypatch.setattr(openwebui, "list_models", lambda ep, key, **kw: [])
    res = webui_router._probe_blocking("openwebui", "http://localhost:8080", "")
    assert res["reachable"] is True
    assert res["endpoint"] == swap8080
    assert res["models"] == []


def test_probe_unreachable_everywhere(monkeypatch) -> None:
    monkeypatch.setattr(_endpoint_fallback, "probe_endpoint", lambda ep, **kw: False)
    monkeypatch.setattr(ollama, "list_models", lambda ep, **kw: [])
    res = webui_router._probe_blocking("ollama", LOCAL, "")
    assert res["reachable"] is False
    assert res["endpoint"] == LOCAL
    assert res["models"] == []


# ---------------------------------------------------------------------------
# Runtime providers: a successful rescue outranks the stale env override
# ---------------------------------------------------------------------------

def test_ollama_rescue_overrides_env_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("CLK_OLLAMA_ENDPOINT", LOCAL)
    monkeypatch.setattr(ollama, "probe_endpoint", lambda ep, **kw: ep == SWAP)
    monkeypatch.setattr(
        ollama, "maybe_docker_host_fallback", lambda ep, **kw: SWAP if ep == LOCAL else None
    )
    prov = ollama.OllamaProvider(name="ollama", config={"type": "ollama", "endpoint": LOCAL})
    assert prov._endpoint() == LOCAL  # env wins before any rescue
    assert prov.available() is True
    # After the rescue, every subsequent call must target the working URL —
    # even though the env var still points at the dead localhost.
    assert prov._endpoint() == SWAP


def test_openwebui_rescue_overrides_env_endpoint(monkeypatch) -> None:
    local = "http://localhost:8080"
    swap = "http://host.docker.internal:8080"
    monkeypatch.setenv("CLK_OPENWEBUI_ENDPOINT", local)
    monkeypatch.setattr(
        _endpoint_fallback, "probe_endpoint", lambda ep, **kw: ep == swap
    )
    prov = openwebui.OpenWebUIProvider(
        name="openwebui", config={"type": "openwebui", "endpoint": local}
    )
    assert prov.available() is True
    assert prov._endpoint() == swap


def test_ollama_no_rescue_keeps_env_endpoint(monkeypatch) -> None:
    monkeypatch.setenv("CLK_OLLAMA_ENDPOINT", LOCAL)
    monkeypatch.setattr(ollama, "probe_endpoint", lambda ep, **kw: True)
    prov = ollama.OllamaProvider(name="ollama", config={"type": "ollama"})
    assert prov.available() is True
    assert prov._endpoint() == LOCAL
