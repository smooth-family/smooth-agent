# Smooth Agent — chat channels + scheduled jobs in a container.
#
#   docker build -t smooth-agent .
#   docker build -t smooth-agent:browser --build-arg BROWSER=1 .   # + Chromium for browser tools
#
# Everything the agent keeps (config, .env keys, memory, sessions, skills,
# workspace) lives in /data — mount a volume there. See docker-compose.yml.
FROM python:3.12-slim

ARG BROWSER=0
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    SMOOTH_AGENT_HOME=/data \
    PLAYWRIGHT_BROWSERS_PATH=/opt/playwright

# git: skills install from git URLs; tini: clean signals so the daemon shuts down gracefully;
# unar + 7zip: RAR and 7z archives; poppler-utils: scanned PDFs rendered to images for vision
RUN apt-get update \
 && apt-get install -y --no-install-recommends git ca-certificates tini unar 7zip poppler-utils \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src
COPY pyproject.toml README.md LICENSE THIRD_PARTY_NOTICES.md ./
COPY smooth_agent ./smooth_agent
RUN if [ "$BROWSER" = "1" ]; then \
      pip install ".[browser]" && python -m playwright install --with-deps chromium; \
    else \
      pip install .; \
    fi \
 && rm -rf /src

RUN useradd --create-home --uid 1000 smooth \
 && mkdir -p /data \
 && chown smooth:smooth /data
COPY --chmod=755 docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
# The entrypoint starts as root only to make a bind-mounted /data writable, then
# drops to the `smooth` user (uid 1000) for the agent itself.
WORKDIR /data
VOLUME ["/data"]

# inbound webhooks (WhatsApp, Slack, LINE). Telegram and Matrix need no open port.
EXPOSE 8808

HEALTHCHECK --interval=60s --timeout=10s --start-period=20s \
  CMD python -c "import sys; from pathlib import Path; from smooth_agent.scheduler import DaemonLock; sys.exit(0 if DaemonLock.running(Path('/data')) else 1)"

ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["daemon"]
