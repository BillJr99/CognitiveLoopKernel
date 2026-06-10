"""Default Archon-style YAML workflows."""

from __future__ import annotations

from typing import Dict


WORKFLOWS: Dict[str, str] = {
    "discovery.yaml": """name: discovery
description: Validate the problem, users, and landscape before building anything.
stages:
  - id: decompose
    agent: chief
    objective: Decompose the discovery objective and identify sub-questions.
    commit: true
  - id: research
    agent: researcher
    objective: Investigate prior art, users, and competitive landscape.
    depends_on: [decompose]
    commit: true
  - id: synthesize
    agent: analyst
    objective: Synthesize research into a one-page brief and update decisions.
    depends_on: [research]
    commit: true
  - id: critique
    agent: critic
    objective: Identify the weakest assumptions in the brief.
    depends_on: [synthesize]
    commit: true
""",

    "product.yaml": """name: product
description: Translate the validated brief into a PRD and prioritized MVP.
stages:
  - id: decompose
    agent: chief
    objective: Decompose product planning into PM + architect tracks.
    commit: true
  - id: prd
    agent: product_manager
    objective: Update the PRD with personas, JTBD, MVP features, success metrics.
    depends_on: [decompose]
    validation: "test -f .clk/state/prd.json"
    commit: true
  - id: architecture
    agent: architect
    objective: Draft the technical architecture aligned with the PRD.
    depends_on: [prd]
    validation: "test -f ARCHITECTURE.md"
    commit: true
  - id: critique
    agent: critic
    objective: Identify gaps between the PRD and architecture.
    depends_on: [architecture]
    commit: true
""",

    "engineering.yaml": """name: engineering
description: |
  Iterative development cycle. The chief overwrites this file on the first
  casting pass with a tailored workflow. Runs continue until the chief
  judges the output exemplary; the supervise stage is always re-added so
  no agent stops until the work is genuinely production-ready.
  Default: 100 supervise cycles available — use them all if needed.
stages:
  - id: cast
    agent: chief
    objective: >
      Decompose the full idea into concrete sub-objectives. Cast or refresh
      the roster (spawn engineer, ralph, qa, researcher, analyst, critic as
      needed). Author a multi-stage project-specific engineering workflow
      that covers ALL aspects of the idea — not just the first slice.
      Plan at least 3-5 substantive stages: parallel research + engineer
      tracks, at least one ralph refinement pass, a qa validation stage,
      and a final supervise stage. Err on the side of more stages and more
      agents rather than fewer.
    commit: true
  - id: implement
    agent: engineer
    objective: >
      Implement the smallest vertical slice that advances the chief's plan.
      Produce working, committed code or content — not a stub or placeholder.
    depends_on: [cast]
    commit: true
  - id: refine
    agent: ralph
    objective: >
      Pick the single highest-value measurable improvement to the engineer's
      output. Implement it, validate it with a shell command, and record
      the finding in PROGRESS.md.
    depends_on: [implement]
    commit: true
  - id: qa
    agent: qa
    objective: >
      Validate the implemented slice thoroughly. Run all available tests.
      Flag every gap, broken assumption, and missing edge case.
    depends_on: [refine]
    commit: true
  - id: supervise
    agent: chief
    objective: >
      Supervise. Is the output exemplary, complete, and production-ready?
      Only emit ACTION:done if the answer is an unambiguous YES.
      Otherwise PROPOSE_WORKFLOW with the next iteration's stages —
      continue improving. Default: keep going.
    depends_on: [qa]
    commit: false
""",

    "validation.yaml": """name: validation
description: Drive the system toward a green test suite.
stages:
  - id: critique
    agent: critic
    objective: Identify the weakest validation in the project.
    commit: true
  - id: qa
    agent: qa
    objective: Strengthen tests and run them.
    depends_on: [critique]
    commit: true
""",

    "deployment.yaml": """name: deployment
description: Produce a runnable deployment recipe and checklist.
stages:
  - id: operate
    agent: operator
    objective: Author or update DEPLOYMENT.md and deployment scripts.
    commit: true
  - id: qa
    agent: qa
    objective: Dry-run the deployment recipe and report.
    depends_on: [operate]
    commit: true
  - id: critique
    agent: critic
    objective: Identify the riskiest deployment gap.
    depends_on: [qa]
    commit: true
""",

    "ralph_loop.yaml": """name: ralph_loop
description: Single Ralph-style iteration. Use clk loop to repeat.
stages:
  - id: ralph
    agent: ralph
    objective: Pick one measurable improvement.
    commit: false
  - id: implement
    agent: engineer
    objective: Implement the Ralph-selected improvement.
    depends_on: [ralph]
    commit: true
  - id: qa
    agent: qa
    objective: Validate the improvement.
    depends_on: [implement]
    commit: true
""",
}
