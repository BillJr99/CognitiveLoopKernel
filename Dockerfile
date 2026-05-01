FROM python:3.11-slim

# git is required for the harness commit operations inside each kickoff dir.
RUN apt-get update && apt-get install -y --no-install-recommends \
        git \
    && rm -rf /var/lib/apt/lists/*

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

# Default to non-interactive pipeline; override with -e CLK_NO_TUI=false -it
# for the full curses TUI (requires a pseudo-terminal).
ENV CLK_NO_TUI=true

ENTRYPOINT ["./kickoff.sh"]
