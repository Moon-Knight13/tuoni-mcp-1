# syntax=docker/dockerfile:1.7@sha256:a57df69d0ea827fb7266491f2813635de6f17269be881f696fbfdf2d83dda33e

ARG PYTHON_IMAGE=python:3.12-slim@sha256:cb33f7f771ecec2ef1225cf39b159591a040391bd73e4c01f00fdfc4c47611d2
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.13.4@sha256:9f130421e930bfbdc563c00740466603f8723158e90297d07957c2294640262b

FROM ${UV_IMAGE} AS uv

FROM ${PYTHON_IMAGE} AS build-base
COPY --from=uv /uv /uvx /bin/

ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

WORKDIR /opt/tuoni-mcp

# Build directly from a standalone checkout and public, locked dependencies.
COPY pyproject.toml uv.lock README.md LICENSE ./
COPY src/ ./src/

FROM build-base AS test-builder
COPY tests/ ./tests/
RUN uv sync --frozen --all-extras --no-editable

FROM build-base AS production-builder
RUN uv sync --frozen --no-dev --no-editable

FROM ${PYTHON_IMAGE} AS runtime-base
ARG PYTHON_IMAGE
LABEL org.opencontainers.image.base.name="${PYTHON_IMAGE}"
ENV PYTHONUNBUFFERED=1 \
    PATH=/opt/tuoni-mcp/.venv/bin:$PATH \
    TUONI_VERIFY_TLS=true \
    TUONI_MCP_PROFILES=agent-ops \
    TUONI_MCP_MODE=native \
    TUONI_MCP_READ_ONLY=false
WORKDIR /opt/tuoni-mcp
# Refresh distro security fixes after resolving the pinned base. The release
# image digest identifies the resulting deployment; audit it before publishing.
RUN apt-get update \
    && apt-get upgrade -y --no-install-recommends \
    && rm -rf /var/lib/apt/lists/* \
    && groupadd --gid 10001 tuoni \
    && useradd --uid 10001 --gid tuoni --create-home tuoni \
    # ncurses-bin is Debian-essential, but MCP never needs its infocmp utility.
    # Remove that vulnerable executable without removing essential packages.
    && rm -f /usr/bin/infocmp \
    # Runtime dependencies are installed in the copied venv. Remove the base
    # installer's vendored CA bundle, including its ensurepip bootstrap copy.
    && rm -rf /usr/local/lib/python3.12/site-packages/pip \
        /usr/local/lib/python3.12/site-packages/pip-*.dist-info \
        /usr/local/lib/python3.12/ensurepip \
        /usr/local/bin/pip /usr/local/bin/pip3 /usr/local/bin/pip3.12 \
    # MCP needs no privileged local helpers, including SUID mount utilities.
    && find / -xdev -type f -perm /6000 -exec chmod a-s {} +
USER 10001:10001

FROM runtime-base AS test
COPY --from=test-builder /opt/tuoni-mcp/.venv /opt/tuoni-mcp/.venv
COPY pyproject.toml uv.lock ./
COPY scripts/check_release.py ./scripts/check_release.py
COPY scripts/check_dist.py ./scripts/check_dist.py
COPY scripts/check_image.py scripts/image_risks.json ./scripts/
COPY scripts/docker/Dockerfile scripts/docker/Dockerfile.dockerignore ./scripts/docker/
COPY scripts/env/default.env scripts/env/override.env.example ./scripts/env/
COPY docs/ ./docs/
COPY evaluation/ ./evaluation/
COPY .agents/ ./.agents/
COPY .github/ ./.github/
COPY tests/ ./tests/
ENTRYPOINT ["python", "-m", "pytest"]
CMD ["-q", "-o", "cache_dir=/tmp/pytest-cache", "tests"]

FROM runtime-base AS production
COPY --from=production-builder /opt/tuoni-mcp/.venv /opt/tuoni-mcp/.venv
ENTRYPOINT ["tuoni-mcp"]
