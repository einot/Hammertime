.PHONY: setup test lint typecheck fmt up down load replay bench

setup:
	uv sync --all-extras --dev

test:
	uv run pytest -q

lint:
	uv run ruff check .

fmt:
	uv run ruff format .

typecheck:
	uv run mypy packages services

up:
	docker compose -f deploy/docker-compose.yml up --build -d

down:
	docker compose -f deploy/docker-compose.yml down -v

load:
	uv run hammertime-agent-sim --target http://localhost:8080 --agents 16 --prefix 10.20.30.0/24

replay:
	uv run hammertime-replay --topic hammertime.hot-ip.v1 --from-beginning

bench:
	uv run pytest tests/bench -q --benchmark-only
