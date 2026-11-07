# syntax=docker/dockerfile:1

# ------------------------------------------------------------------------------
# Stage 1: Build & Dependencies
# ------------------------------------------------------------------------------
FROM python:3.12-slim AS builder

WORKDIR /build

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    curl \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

# Install dependencies into virtual environment
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

COPY pyproject.toml uv.lock* ./

RUN uv sync --frozen --no-dev || uv sync --no-dev

# ------------------------------------------------------------------------------
# Stage 2: Production Runtime
# ------------------------------------------------------------------------------
FROM python:3.12-slim AS runtime

ARG UID=1000
ARG GID=1000

# Create non-root user and group
RUN groupadd -g "${GID}" swiftpy \
    && useradd -u "${UID}" -g swiftpy -s /bin/bash -m swiftpy

WORKDIR /app

# Copy virtualenv and binaries from builder
COPY --from=builder --chown=swiftpy:swiftpy /build/.venv /app/.venv

# Copy application source code
COPY --chown=swiftpy:swiftpy . /app

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1

USER swiftpy

EXPOSE 8000

CMD ["swiftpy", "start", "--host", "0.0.0.0", "--port", "8000"]
