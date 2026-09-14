FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock* ./
COPY packages ./packages
COPY services ./services
RUN uv sync --frozen --no-dev --package hammertime-trie
ENTRYPOINT ["uv", "run", "hammertime-trie"]
