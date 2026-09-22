FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:0.12.17 /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock* ./
COPY packages ./packages
COPY tools ./tools
RUN uv sync --frozen --no-dev --package hammertime-provision
ENTRYPOINT ["uv", "run", "hammertime-provision"]
