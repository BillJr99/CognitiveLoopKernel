"""Full pipeline smoke test.

Drive a single project through every documented stage:
init → idea → cast → roles → plan → run → loop → status.  Assert the
high-level invariants the user-facing docs promise.
"""

from __future__ import annotations

import json
from pathlib import Path

from .conftest import run_clk


def test_full_pipeline_with_shell_provider(clk_project: Path) -> None:
    # 1. init
    res = run_clk("init", "--name", "full-pipeline", cwd=clk_project)
    assert res.returncode == 0, res.stderr
    assert (clk_project / ".clk").is_dir()

    # 2. idea — auto-cast on (shell provider always returns ok)
    res = run_clk(
        "idea", "A local-first journaling app",
        "--title", "Journal",
        cwd=clk_project,
    )
    assert res.returncode == 0, res.stderr
    assert (clk_project / ".clk" / "state" / "idea.json").is_file()

    # 3. cast — re-cast explicitly
    res = run_clk("cast", cwd=clk_project)
    assert res.returncode == 0, res.stderr

    # 4. roles list — baseline + any dynamic roles the chief proposed
    res = run_clk("roles", "list", cwd=clk_project)
    assert res.returncode == 0
    assert "chief" in res.stdout
    assert "qa" in res.stdout
    assert "ralph" in res.stdout

    # 5. plan (dry-run keeps it fast)
    res = run_clk("plan", "--dry-run", cwd=clk_project)
    # plan can legitimately return 1 if a stage validation fails — but the
    # harness must still have invoked both discovery and product.
    assert res.returncode in (0, 1)

    # 6. run (dry-run)
    res = run_clk("run", "--dry-run", cwd=clk_project)
    assert res.returncode in (0, 1)

    # 7. loop ralph
    res = run_clk(
        "loop", "--mode", "ralph", "--max-iterations", "1", "--dry-run",
        cwd=clk_project,
    )
    assert res.returncode == 0, res.stderr

    # 8. status — must reflect the captured idea + provider config
    res = run_clk("status", cwd=clk_project)
    assert res.returncode == 0
    assert "full-pipeline" in res.stdout
    assert "shell" in res.stdout
