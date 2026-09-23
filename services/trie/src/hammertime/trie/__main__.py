"""Entry point: hand the trie's composition root to the shared runner.

Spec: section 33, section 47; ADR-0009 decision 1, ADR-0017 decision 13.

`main()` takes no arguments, reads no `argv`, and does nothing but turn
`run_service`'s exit code into the process status -- 0 clean shutdown, 1
runtime failure, 2 configuration invalid. Structured logging, the
`starting`/`ready` records, the startup deadline (inside which the replay of
section 33 runs), signals and the drain belong to `hammertime.core.runtime`;
the object graph belongs to `hammertime.trie.service.build_service`.

`load_settings()` runs inside the factory, so a malformed environment
variable is one `config_invalid` record and exit 2, before any bus or socket
is opened. Until the snapshot epic lands, every start replays the whole
retained hot-ip log; with it, the replay starts after the snapshot's
position.
"""

from hammertime.core.runtime import run_service
from hammertime.trie.config import load_settings
from hammertime.trie.service import SERVICE_NAME, TrieService, build_service


def _build_from_env() -> TrieService:
    """The composition root partially applied to `os.environ`."""
    return build_service(load_settings())


def main() -> None:
    raise SystemExit(run_service(SERVICE_NAME, _build_from_env))


if __name__ == "__main__":
    main()
