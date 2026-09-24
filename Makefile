.PHONY: setup test lint typecheck fmt up down load replay bench

setup:
	uv sync --all-extras --dev

test:
	uv run --locked pytest -q

lint:
	uv run --locked ruff check .

fmt:
	uv run --locked ruff format .

typecheck:
	uv run --locked mypy packages services tools

up:
	docker compose -f deploy/docker-compose.yml up --build -d --wait

down:
	docker compose -f deploy/docker-compose.yml down -v

load:
	uv run --locked hammertime-agent-sim --target http://localhost:8080 --agents 16 --prefix 10.20.30.0/24

replay:
	uv run --locked hammertime-replay --topic hammertime.hot-ip.v1 --from-beginning

bench:
	uv run --locked pytest tests/bench -q --benchmark-only
