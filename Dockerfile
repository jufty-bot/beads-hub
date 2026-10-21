FROM ghcr.io/astral-sh/uv:0.12.1 AS uv
FROM node:22-bookworm-slim

ARG TARGETARCH
ARG BEADS_VERSION=1.3.1
ARG DOLT_VERSION=2.5.1
ARG BEADS_UI_VERSION=0.12.6

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates curl git python3 gh tini \
    && rm -rf /var/lib/apt/lists/*
COPY --from=uv /uv /usr/local/bin/uv

RUN set -eu; \
    curl -fSL "https://github.com/gastownhall/beads/releases/download/v${BEADS_VERSION}/beads_${BEADS_VERSION}_linux_${TARGETARCH}.tar.gz" -o /tmp/beads.tar.gz; \
    curl -fSL "https://github.com/gastownhall/beads/releases/download/v${BEADS_VERSION}/checksums.txt" -o /tmp/checksums.txt; \
    cd /tmp; \
    expected=$(awk -v file="beads_${BEADS_VERSION}_linux_${TARGETARCH}.tar.gz" '$2 == file { print $1 }' checksums.txt); \
    test -n "$expected"; \
    echo "$expected  beads.tar.gz" | sha256sum --check; \
    tar -xzf beads.tar.gz bd; \
    install bd /usr/local/bin/bd; \
    curl -fSL "https://github.com/dolthub/dolt/releases/download/v${DOLT_VERSION}/dolt-linux-${TARGETARCH}.tar.gz" -o /tmp/dolt.tar.gz; \
    tar -xzf /tmp/dolt.tar.gz; \
    install "dolt-linux-${TARGETARCH}/bin/dolt" /usr/local/bin/dolt; \
    rm -rf /tmp/*

WORKDIR /opt/beads-hub
COPY pyproject.toml uv.lock ./
RUN uv sync --locked --no-dev --no-install-project --python /usr/bin/python3
RUN npm install -g "beads-ui@${BEADS_UI_VERSION}" && npm cache clean --force
COPY scripts/ /usr/local/bin/
ENV PATH="/opt/beads-hub/.venv/bin:$PATH" \
    BEADS_PATH=/usr/local/bin/bd \
    BEADS_WORKING_DIR=/var/lib/beads \
    BEADS_DIR=/var/lib/beads/.beads \
    BEADS_DOLT_SERVER_MODE=1 \
    BEADS_DOLT_SERVER_HOST=127.0.0.1 \
    BEADS_DOLT_SERVER_PORT=3307 \
    BEADS_DOLT_AUTO_START=0 \
    BEADS_ACTOR=beads-hub \
    GH_PROMPT_DISABLED=1 \
    BD_BIN=/usr/local/bin/bd
WORKDIR /var/lib/beads
EXPOSE 3000 3307 8080
ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/entrypoint-hub.sh"]
