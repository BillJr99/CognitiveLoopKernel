"""End-to-end smoke tests for `clk doctor` and `clk diag`.

These spin up a temporary CLK workspace, run the subcommand, and assert
the output shape + exit code. They avoid hitting any real provider.
"""

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_clk(*args, cwd: Path) -> subprocess.CompletedProcess:
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["CLK_DISABLE_API"] = "1"  # don't auto-start the API server in tests
    return subprocess.run(
        [sys.executable, "-m", "clk_harness.cli", *args],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def test_clk_doctor_clean_init_returns_zero(tmp_path):
    """A fresh shell-provider workspace passes every check."""
    proc = _run_clk("init", "--name", "doctest", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr

    proc = _run_clk("doctor", cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert "All checks passed" in proc.stdout
    assert "active_provider: shell" in proc.stdout


def test_clk_doctor_reports_unavailable_active_provider(tmp_path):
    """Switch active to a provider that isn't installed → doctor fails."""
    _run_clk("init", "--name", "doctest", cwd=tmp_path)
    # Switch active to "pi" — won't be installed in CI.
    cfg_path = tmp_path / ".clk" / "config" / "providers.json"
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["active"] = "pi"
    cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    proc = _run_clk("doctor", cwd=tmp_path)
    # Active provider unavailable → exit 1.
    assert proc.returncode == 1, proc.stdout
    assert "fail" in proc.stdout
    assert "pi" in proc.stdout


def test_clk_doctor_fix_prints_suggestions(tmp_path):
    """--fix surfaces actionable suggestions without executing them."""
    _run_clk("init", cwd=tmp_path)
    cfg_path = tmp_path / ".clk" / "config" / "providers.json"
    data = json.loads(cfg_path.read_text(encoding="utf-8"))
    data["active"] = "ollama"
    cfg_path.write_text(json.dumps(data, indent=2), encoding="utf-8")
    proc = _run_clk("doctor", "--fix", cwd=tmp_path)
    # Still exit 1, but mentions install_tool.
    assert proc.returncode == 1, proc.stdout
    assert "install_tool" in proc.stdout


def test_clk_diag_writes_redacted_tarball(tmp_path):
    """Diag produces a tarball and the .env is redacted."""
    _run_clk("init", cwd=tmp_path)
    # Plant a fake API key in .env.
    (tmp_path / ".env").write_text(
        "ANTHROPIC_API_KEY=sk-not-a-real-secret-1234567890\n"
        "OPENAI_API_KEY=sk-also-fake-abcdef\n"
        "CLK_PROVIDER=shell\n",
        encoding="utf-8",
    )
    proc = _run_clk("diag", cwd=tmp_path)
    assert proc.returncode == 0, proc.stderr
    # Tarball lives at the project root.
    tarballs = list(tmp_path.glob("clk-diag-*.tar.gz"))
    assert tarballs, f"no tarball: stdout={proc.stdout} stderr={proc.stderr}"
    # Open and confirm the .env was redacted.
    import tarfile
    with tarfile.open(tarballs[0]) as tf:
        names = tf.getnames()
        assert ".env.redacted" in names
        f = tf.extractfile(".env.redacted")
        body = f.read().decode("utf-8")
        assert "sk-not-a-real-secret" not in body
        assert "sk-also-fake" not in body
        assert "redacted:" in body
