FROM python:3.11-slim

# System deps: git (harness commits), curl/gnupg/ca-certificates (NodeSource setup).
RUN apt-get update && apt-get install -y --no-install-recommends \
        git curl ca-certificates gnupg \
    && rm -rf /var/lib/apt/lists/*

# Node.js LTS via NodeSource (needed for claude, codex, gemini, pi CLIs).
RUN curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
       | gpg --dearmor -o /usr/share/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/usr/share/keyrings/nodesource.gpg] https://deb.nodesource.com/node_lts.x nodistro main" \
       > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends nodejs \
    && rm -rf /var/lib/apt/lists/*

# Provider CLIs — installed globally so they're on PATH for all agents.
RUN npm install -g \
        @anthropic-ai/claude-code \
        @openai/codex \
        @google/gemini-cli \
        pi

WORKDIR /app

# Install Python dependencies before copying sources so this layer is cached.
COPY pyproject.toml ./
RUN pip install --no-cache-dir pyyaml

# Copy harness sources.
COPY clk_harness/ ./clk_harness/
COPY scripts/     ./scripts/
COPY kickoff.sh   ./
COPY .env.example ./

RUN chmod +x kickoff.sh scripts/clk scripts/install_local.sh scripts/run_loop.sh 2>/dev/null || true

# Global git identity used by the harness when committing inside kickoff dirs.
RUN git config --global user.name  "CLK Container" \
 && git config --global user.email "clk@local.invalid"

# Kickoffs are created under workspace/ — mount a volume here to keep them
# after the container exits, or bind-mount a host directory instead.
RUN mkdir -p workspace
VOLUME /app/workspace

# Run with -it for the interactive TUI (the default). For non-interactive
# use (CI, scripting) pass -e CLK_NO_TUI=true and omit -it.
ENTRYPOINT ["./kickoff.sh"]
