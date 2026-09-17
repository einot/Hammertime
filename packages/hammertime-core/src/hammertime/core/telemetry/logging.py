"""Structured logging setup; correlation on agent_id, shard, and event sequence.

Spec: section 37, section 47

One JSON object per line on stdout, every record carrying `service=<name>`
(ADR-0009 decision 5 step 1) so a container log stream can be split by
service without parsing a prefix. Records emitted through the standard
library -- FastAPI, uvicorn, and this repo's own `logging.getLogger(...)`
call sites -- are rendered by the same processor chain as structlog's own,
so a service never mixes two log formats on one stream.

Metric *content* (spec section 37's counters and histograms) belongs to
`telemetry/metrics.py` and the telemetry epic; this module only settles how
an event is rendered and what every event carries.
"""

import logging
import sys
from typing import cast

import structlog
from structlog.typing import EventDict, Processor, WrappedLogger

#: Default for HAMMERTIME_LOG_LEVEL (`.env.example`).
DEFAULT_LEVEL = "info"


def _resolve_level(level: str) -> int:
    """Map a HAMMERTIME_LOG_LEVEL value onto a `logging` level number.

    Raises `ValueError` naming the variable, the same way every
    `load_settings` rejects a malformed value (ADR-0009 decision 2), so the
    runner can report it as `config_invalid` and exit 2 rather than letting
    a typo silently downgrade a deployment's observability.
    """
    names = logging.getLevelNamesMapping()
    resolved = names.get(level.strip().upper())
    if resolved is None:
        allowed = sorted(name.lower() for name in names if name != "NOTSET")
        raise ValueError(f"HAMMERTIME_LOG_LEVEL must be one of {allowed}, got {level!r}")
    return resolved


def _service_processor(name: str) -> Processor:
    """A processor stamping `service=<name>` on every record that lacks one."""

    def add_service(_logger: WrappedLogger, _method_name: str, event_dict: EventDict) -> EventDict:
        event_dict.setdefault("service", name)
        return event_dict

    return add_service


def configure_logging(name: str, level: str = DEFAULT_LEVEL) -> None:
    """Configure process-wide structured logging for service `name`.

    Idempotent: calling it again reconfigures both structlog and the root
    stdlib logger (replacing this module's handler) rather than stacking a
    second handler and doubling every line.
    """
    numeric_level = _resolve_level(level)

    # Shared by structlog's own chain and by the `foreign_pre_chain` that
    # dresses stdlib records, so both carry the same keys.
    shared: list[Processor] = [
        structlog.contextvars.merge_contextvars,
        _service_processor(name),
        structlog.processors.add_log_level,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
    ]

    structlog.configure(
        processors=[
            *shared,
            structlog.processors.StackInfoRenderer(),
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        # False so a later configure_logging() call (a test, or a service
        # re-reading HAMMERTIME_LOG_LEVEL) actually takes effect.
        cache_logger_on_first_use=False,
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        foreign_pre_chain=shared,
        processors=[
            structlog.stdlib.ProcessorFormatter.remove_processors_meta,
            structlog.processors.format_exc_info,
            structlog.processors.JSONRenderer(sort_keys=True),
        ],
    )
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)

    root = logging.getLogger()
    for existing in list(root.handlers):
        root.removeHandler(existing)
    root.addHandler(handler)
    root.setLevel(numeric_level)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """A bound logger rendered by `configure_logging`'s chain."""
    logger = structlog.get_logger() if name is None else structlog.get_logger(name)
    return cast(structlog.stdlib.BoundLogger, logger)
