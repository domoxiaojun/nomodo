# syntax=docker/dockerfile:1
FROM python:3.12-slim-bookworm AS build
COPY --from=ghcr.io/astral-sh/uv:0.10.11 /uv /usr/local/bin/uv
WORKDIR /app
ENV UV_PROJECT_ENVIRONMENT=/opt/venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 UV_PYTHON_DOWNLOADS=never
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends gcc python3-dev \
    && rm -rf /var/lib/apt/lists/*
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
# ParseHub matcher imports its registry; OpenCV needs glib even though parsing runs remotely.
# hadolint ignore=DL3008
RUN apt-get update && apt-get install -y --no-install-recommends libglib2.0-0 ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ENV PATH="/opt/venv/bin:$PATH" PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
WORKDIR /app
COPY --from=build /opt/venv /opt/venv
CMD ["python", "-m", "simpread"]
