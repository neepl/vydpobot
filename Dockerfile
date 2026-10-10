FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim AS builder
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
WORKDIR /app
# Dependencies first (cached layer), then the project itself.
COPY pyproject.toml uv.lock README.md ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable --no-install-project
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev --no-editable

FROM python:3.12-slim-bookworm
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    MPLCONFIGDIR=/tmp/matplotlib
RUN useradd --system --uid 10001 --home-dir /app --no-create-home bot \
    && mkdir -p /app/data && chown bot:bot /app/data
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
USER bot
VOLUME /app/data
CMD ["deye-bot", "run"]
