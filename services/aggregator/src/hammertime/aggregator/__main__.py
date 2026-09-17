"""Entry point: hand the aggregator's composition root to the shared runner.

Spec: section 20, section 47; ADR-0009, ADR-0011

ADR-0009 decision 1: `main()` takes no arguments, reads no `argv`, and does
nothing but turn `run_service`'s exit code into the process status -- 0 clean
shutdown, 1 runtime failure, 2 configuration invalid. Everything else
(structured logging, the `starting`/`ready` records, the startup deadline and
its dependency backoff, SIGTERM/SIGINT, the drain) belongs to
`hammertime.core.runtime`, and the object graph belongs to
`hammertime.aggregator.service.build_service`.

`load_settings()` runs inside the factory, so a malformed environment
variable -- including a set-but-empty `HAMMERTIME_SHARD_IDS` (ADR-0011
Amendment 1, item A3) -- is reported as one `config_invalid` record and exit
2, before any bus, store or socket is opened.
"""

from hammertime.aggregator.config import load_settings
from hammertime.aggregator.service import SERVICE_NAME, AggregatorService, build_service
from hammertime.core.runtime import run_service


def _build_from_env() -> AggregatorService:
    """The composition root partially applied to `os.environ`."""
    return build_service(load_settings())


def main() -> None:
    raise SystemExit(run_service(SERVICE_NAME, _build_from_env))


if __name__ == "__main__":
    main()
