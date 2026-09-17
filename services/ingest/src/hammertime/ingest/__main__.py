"""Entry point: hand ingest's composition root to the shared runner.

Spec: section 4, section 47; ADR-0007, ADR-0009

ADR-0009 decision 1: `main()` takes no arguments, reads no `argv`, and does
nothing but turn `run_service`'s exit code into the process status --
0 clean shutdown, 1 runtime failure, 2 configuration invalid. Everything
else (structured logging, the `starting`/`ready` records, the startup
deadline and its dependency backoff, SIGTERM/SIGINT, the drain) belongs to
`hammertime.core.runtime`, and the object graph belongs to
`hammertime.ingest.service.build_service`.

`load_settings()` runs inside the factory, so a malformed environment
variable is reported as one `config_invalid` record and exit 2 rather than
as a `ValueError` traceback.

The HTTP server itself is built in `service.py`, which keeps uvicorn's own
`X-Forwarded-For`/`X-Real-IP` rewriting disabled (`proxy_headers=False`):
interpreting those headers is solely `auth/middleware.py`'s
`trusted_proxy_hops` logic (spec section 36.5), never two layers that can
disagree. The reasoning is recorded in full at the setting itself.
"""

from hammertime.core.runtime import run_service
from hammertime.ingest.config import load_settings
from hammertime.ingest.service import SERVICE_NAME, IngestService, build_service


def _build_from_env() -> IngestService:
    """`build_from_env`: the composition root partially applied to `os.environ`."""
    return build_service(load_settings())


def main() -> None:
    raise SystemExit(run_service(SERVICE_NAME, _build_from_env))


if __name__ == "__main__":
    main()
