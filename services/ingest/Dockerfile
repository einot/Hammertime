FROM python:3.12-slim
COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock* ./
COPY packages ./packages
COPY services ./services
# validation/schema.py loads schemas/observation.v1.json from disk at
# startup, and the default HAMMERTIME_CONFIG_PATH (./config/detection.v1.json)
# is resolved relative to WORKDIR -- both must exist in the image.
COPY schemas ./schemas
COPY config ./config
RUN uv sync --frozen --no-dev --package hammertime-ingest
ENTRYPOINT ["uv", "run", "hammertime-ingest"]
