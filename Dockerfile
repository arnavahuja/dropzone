# Container image for Cloud Run.
#
# Build with Cloud Build (`gcloud run deploy --source .`) or locally with
# `docker build -t dropzone .`.

FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app

# Copy bytecode rather than hardlinking, and precompile it: both make a cold
# start on Cloud Run noticeably faster.
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PYTHONUNBUFFERED=1

# Dependencies first, so a code change does not re-resolve the lock file.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

COPY app.py ./
COPY dropzone ./dropzone
COPY data ./data
COPY static ./static

# Run from the virtualenv directly: no `uv run`, which would re-check the lock
# on every container start.
ENV PATH="/app/.venv/bin:$PATH"

# Cloud Run injects PORT and expects the server on 0.0.0.0. The single worker is
# deliberate: sessions live in this process's memory (see README, Deploying).
EXPOSE 8080
CMD ["/bin/sh", "-c", "exec uvicorn app:app --host 0.0.0.0 --port ${PORT:-8080} --workers 1"]
