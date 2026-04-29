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
description: One full development cycle.
stages:
  - id: decompose
    agent: chief
    objective: Decompose the next slice and assign work.
    commit: true
  - id: research
    agent: researcher
    objective: Resolve any technical assumptions for the slice.
    depends_on: [decompose]
    commit: true
  - id: prd_update
    agent: product_manager
    objective: Update the PRD if scope changed.
    depends_on: [decompose]
    commit: true
  - id: architect
    agent: architect
    objective: Update architecture if the slice changes shape.
    depends_on: [decompose]
    commit: true
  - id: implement
    agent: engineer
    objective: Implement the smallest vertical slice.
    depends_on: [architect, prd_update, research]
    commit: true
  - id: qa
    agent: qa
    objective: Test and audit the implemented slice.
    depends_on: [implement]
    commit: true
  - id: critique
    agent: critic
    objective: Identify the next gap to close.
    depends_on: [qa]
    commit: true
  - id: operate
    agent: operator
    objective: Update deployment artifacts to reflect the slice.
    depends_on: [qa]
    commit: true
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
