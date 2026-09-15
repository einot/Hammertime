"""JSON Schema validation; reject unknown fields and any client-asserted state.

Spec: section 36
"""

import json
from pathlib import Path
from typing import Any

import jsonschema
from hammertime.ingest.validation import SchemaValidationError

_SCHEMA_FILENAME = "observation.v1.json"


def _find_schema_path(filename: str = _SCHEMA_FILENAME) -> Path:
    """Locate `schemas/<filename>` by walking up from this file.

    The repo keeps `schemas/` at the repo root, outside every package, so
    there is no importable resource path for it; this walk works for local
    development, `uv run`, and the test suite, all of which run from within
    a checkout of the repo.
    """
    for candidate in Path(__file__).resolve().parents:
        schema_path = candidate / "schemas" / filename
        if schema_path.is_file():
            return schema_path
    raise FileNotFoundError(f"could not locate schemas/{filename} above {__file__}")


class ObservationSchemaValidator:
    """Validates raw request bodies against schemas/observation.v1.json.

    Compiles the schema once at construction time, not per request. Draft
    2020-12's `additionalProperties: false` is enforced strictly, so an
    unknown field -- including a client-asserted `"state"` key (spec section
    36: "an untrusted agent MUST NOT be able to arbitrarily declare IP =
    HOT") -- fails validation rather than being silently dropped.
    """

    def __init__(self, schema: dict[str, Any]) -> None:
        self._validator = jsonschema.Draft202012Validator(schema)

    @classmethod
    def from_file(cls, path: Path | None = None) -> "ObservationSchemaValidator":
        """Load and compile the schema from `path` (default: schemas/observation.v1.json)."""
        schema_path = path if path is not None else _find_schema_path()
        with schema_path.open("r", encoding="utf-8") as handle:
            schema = json.load(handle)
        return cls(schema)

    def validate(self, document: Any) -> None:
        """Raise `SchemaValidationError` if `document` violates the schema."""
        errors = sorted(self._validator.iter_errors(document), key=lambda e: list(map(str, e.path)))
        if errors:
            messages = "; ".join(f"{list(error.path)}: {error.message}" for error in errors)
            raise SchemaValidationError(f"observation payload failed schema validation: {messages}")
