"""CLI entry point: generate, hash, rotate, and migrate agent credentials.

Spec: section 36.4, ADR-0006

`hammertime-agent-token` is the only component that ever sees an agent's
bearer token in readable form. Every subcommand reads the deployment key
that `config/agents.v2.json`'s hashes are computed under
(`HAMMERTIME_INGEST_AGENT_TOKEN_KEY`, or `--key` for testability) and fails
loudly without one.

    hammertime-agent-token new-key
    hammertime-agent-token key-id [--key KEY | --key-file PATH]
    hammertime-agent-token generate --agent-id ID [--rate-limit-rps N] [--disabled]
                                     [--registry PATH [--replace]] [--key KEY | --key-file PATH]
    hammertime-agent-token hash --agent-id ID [--allow-weak] [--key KEY | --key-file PATH]
    hammertime-agent-token rotate --agent-id ID --registry PATH
                                   [--overlap-hours 24] [--drop-previous]
                                   [--key KEY | --key-file PATH]
    hammertime-agent-token migrate --in config/agents.v1.json --out config/agents.v2.json
                                    [--force] [--key KEY | --key-file PATH]

Every subcommand that takes `--key` also accepts `--key-file PATH` (the key
read from the file, trailing whitespace stripped) as an out-of-band
alternative: passing the deployment key on the command line with `--key`
lands in `ps`, shell history, and CI logs, which is not "out of band" in
any useful sense. At most one of `--key`/`--key-file` may be given;
precedence when neither is given is the `HAMMERTIME_INGEST_AGENT_TOKEN_KEY`
environment variable, which remains the primary, recommended path.

Registry files are edited as plain JSON here, not through
`hammertime.ingest.auth.agents`'s loader: this tool depends only on
`hammertime-core` (the shared `hammertime.core.auth.tokens` primitives), not
on `hammertime-ingest`, so the registry's full parse/validate contract stays
owned by ingest.
"""

import argparse
import contextlib
import json
import os
import sys
import tempfile
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from hammertime.core.auth import tokens
from hammertime.core.errors import ConfigurationError, HammertimeError

_AGENT_TOKEN_KEY_ENV = "HAMMERTIME_INGEST_AGENT_TOKEN_KEY"
#: `hash`'s one enforcement point for token strength (ADR-0006): the server
#: can no longer judge a token it only ever sees hashed, so this is the only
#: place in the whole system a strength rule can be applied at all.
_MIN_TOKEN_CHARS = 32


def _resolve_key(args: argparse.Namespace) -> bytes:
    key_arg = getattr(args, "key", None)
    key_file = getattr(args, "key_file", None)
    if key_arg and key_file:
        raise ConfigurationError("pass at most one of --key or --key-file, not both")

    if key_arg:
        raw = key_arg
    elif key_file:
        try:
            raw = key_file.read_text().strip()
        except OSError as exc:
            raise ConfigurationError(f"cannot read --key-file {key_file}: {exc}") from exc
    else:
        raw = os.environ.get(_AGENT_TOKEN_KEY_ENV)

    if not raw:
        raise ConfigurationError(
            f"no deployment key: pass --key, pass --key-file, or set {_AGENT_TOKEN_KEY_ENV}"
        )
    return tokens.decode_key(raw)


def _registry_entry(
    token_hash_hex: str,
    *,
    enabled: bool = True,
    rate_limit_rps: int | None = None,
    previous_token_hash_hex: str | None = None,
    previous_token_expires_at: str | None = None,
) -> dict[str, Any]:
    entry: dict[str, Any] = {"token_hash": token_hash_hex}
    if previous_token_hash_hex is not None:
        entry["previous_token_hash"] = previous_token_hash_hex
        entry["previous_token_expires_at"] = previous_token_expires_at
    entry["enabled"] = enabled
    entry["rate_limit_rps"] = rate_limit_rps
    return entry


def _print_entry_snippet(agent_id: str, entry: dict[str, Any]) -> None:
    print(json.dumps({agent_id: entry}, indent=2))


def _validate_rate_limit(rate_limit_rps: int | None) -> None:
    if rate_limit_rps is not None and rate_limit_rps <= 0:
        raise ConfigurationError(
            f"--rate-limit-rps must be a positive integer, got {rate_limit_rps!r}"
        )


def _atomic_write_json(path: Path, document: dict[str, Any]) -> None:
    """Write `document` to `path` atomically (temp file + rename), mode 0600."""
    directory = path.parent if str(path.parent) else Path()
    fd, tmp_name = tempfile.mkstemp(dir=str(directory), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(document, handle, indent=2)
            handle.write("\n")
        os.chmod(tmp_name, 0o600)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp_name)
        raise


def _duplicate_key_hook(path: Path) -> Any:
    """`object_pairs_hook` for `json.loads`: raise on a duplicate key anywhere in the document.

    Plain `json.loads` silently keeps only the last occurrence of a
    duplicate key (e.g. two `"some-agent-id": {...}` entries under
    `"agents"`), which would shadow the first entry with no error -- an
    operator reading the top of the file would never see the entry
    actually in effect. Mirrors `hammertime.ingest.auth.agents`'s loader.
    """

    def hook(pairs: Sequence[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise ConfigurationError(f"{path}: duplicate key {key!r} in JSON document")
            result[key] = value
        return result

    return hook


def _read_json_document(path: Path) -> Any:
    """Read and parse a JSON document at `path`, raising `ConfigurationError` on any failure."""
    try:
        raw = path.read_text()
    except OSError as exc:
        raise ConfigurationError(f"cannot read {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ConfigurationError(f"{path} is not valid UTF-8: {exc}") from exc

    try:
        return json.loads(raw, object_pairs_hook=_duplicate_key_hook(path))
    except json.JSONDecodeError as exc:
        raise ConfigurationError(f"{path} is not valid JSON: {exc}") from exc


def _new_envelope(key: bytes) -> dict[str, Any]:
    return {
        "registry_version": tokens.REGISTRY_VERSION,
        "hash_algorithm": tokens.HASH_ALGORITHM,
        "key_id": tokens.derive_key_id(key),
        "agents": {},
    }


def _load_envelope(path: Path, *, key: bytes) -> dict[str, Any]:
    """Load an existing v2 registry document, or a fresh empty envelope if absent.

    Verifies the document's `key_id` matches `key`'s fingerprint before this
    tool ever writes into it -- inserting a hash computed under one key into
    a registry keyed under another would silently produce credentials that
    can never authenticate.
    """
    if not path.exists():
        return _new_envelope(key)

    document = _read_json_document(path)
    if not isinstance(document, dict):
        raise ConfigurationError(f"{path}: registry document must be a JSON object")
    if document.get("registry_version") != tokens.REGISTRY_VERSION:
        raise ConfigurationError(
            f"{path}: registry_version={document.get('registry_version')!r}, "
            f"expected {tokens.REGISTRY_VERSION} -- convert it first with the "
            "'migrate' subcommand"
        )
    if document.get("hash_algorithm") != tokens.HASH_ALGORITHM:
        raise ConfigurationError(
            f"{path}: hash_algorithm={document.get('hash_algorithm')!r}, "
            f"expected {tokens.HASH_ALGORITHM!r}"
        )
    expected_key_id = tokens.derive_key_id(key)
    if document.get("key_id") != expected_key_id:
        raise ConfigurationError(
            f"{path}: key_id={document.get('key_id')!r} does not match the "
            f"configured key (expected {expected_key_id!r}) -- this registry "
            "was hashed under a different HAMMERTIME_INGEST_AGENT_TOKEN_KEY"
        )
    document.setdefault("agents", {})
    return document


def _cmd_new_key(args: argparse.Namespace) -> int:
    print(tokens.generate_token())
    return 0


def _cmd_key_id(args: argparse.Namespace) -> int:
    key = _resolve_key(args)
    print(tokens.derive_key_id(key))
    return 0


def _cmd_generate(args: argparse.Namespace) -> int:
    _validate_rate_limit(args.rate_limit_rps)
    key = _resolve_key(args)

    token = tokens.generate_token()
    entry = _registry_entry(
        tokens.hash_token(token, key=key),
        enabled=not args.disabled,
        rate_limit_rps=args.rate_limit_rps,
    )

    # The token is printed exactly once, here -- the only place in the
    # system it is ever displayed in readable form (ADR-0006).
    print(token)
    _print_entry_snippet(args.agent_id, entry)

    if args.registry is not None:
        document = _load_envelope(args.registry, key=key)
        agents = document["agents"]
        if args.agent_id in agents and not args.replace:
            raise ConfigurationError(
                f"agent {args.agent_id!r} already exists in {args.registry} "
                "-- pass --replace to overwrite"
            )
        agents[args.agent_id] = entry
        _atomic_write_json(args.registry, document)
    return 0


def _cmd_hash(args: argparse.Namespace) -> int:
    key = _resolve_key(args)
    token = sys.stdin.read().strip()
    if not token:
        raise ConfigurationError("no token read from stdin")
    if len(token) < _MIN_TOKEN_CHARS:
        if not args.allow_weak:
            raise ConfigurationError(
                f"token is {len(token)} characters, shorter than the required "
                f"{_MIN_TOKEN_CHARS} -- pass --allow-weak to override"
            )
        # ADR-0006: --allow-weak is meant to be "an explicit, logged escape
        # hatch rather than a silent one" -- never bypass the strength check
        # without a trace of it having happened.
        print(
            "warning: --allow-weak used, credential does not meet minimum strength requirements",
            file=sys.stderr,
        )

    entry = _registry_entry(tokens.hash_token(token, key=key))
    _print_entry_snippet(args.agent_id, entry)
    return 0


def _cmd_rotate(args: argparse.Namespace) -> int:
    key = _resolve_key(args)
    document = _load_envelope(args.registry, key=key)
    agents = document["agents"]
    entry = agents.get(args.agent_id)
    if entry is None:
        raise ConfigurationError(f"agent {args.agent_id!r} not found in {args.registry}")

    if args.drop_previous:
        entry.pop("previous_token_hash", None)
        entry.pop("previous_token_expires_at", None)
        _atomic_write_json(args.registry, document)
        print(f"dropped previous credential for agent {args.agent_id!r}", file=sys.stderr)
        return 0

    if "previous_token_hash" in entry:
        raise ConfigurationError(
            f"agent {args.agent_id!r} already has a previous_token_hash from an "
            "unfinished rotation -- finish it (let it expire, or run 'rotate "
            "--drop-previous') before starting another"
        )

    if args.overlap_hours <= 0:
        raise ConfigurationError(f"--overlap-hours must be positive, got {args.overlap_hours!r}")

    new_token = tokens.generate_token()
    expires_at = datetime.now(UTC) + timedelta(hours=args.overlap_hours)
    entry["previous_token_hash"] = entry["token_hash"]
    entry["previous_token_expires_at"] = expires_at.isoformat()
    entry["token_hash"] = tokens.hash_token(new_token, key=key)
    _atomic_write_json(args.registry, document)

    # Printed exactly once, same as 'generate'.
    print(new_token)
    return 0


def _cmd_migrate(args: argparse.Namespace) -> int:
    if args.input_path.resolve() == args.output_path.resolve():
        raise ConfigurationError("--out must be a different file from --in")

    if not args.force and args.output_path.exists() and args.output_path.stat().st_size > 0:
        raise ConfigurationError(
            f"--out {args.output_path} already exists and is non-empty -- refusing to "
            "overwrite it (this could destroy a live populated registry); pass --force "
            "to overwrite it anyway"
        )

    key = _resolve_key(args)
    v1_document = _read_json_document(args.input_path)
    if not isinstance(v1_document, dict):
        raise ConfigurationError(f"{args.input_path}: registry document must be a JSON object")

    agents: dict[str, Any] = {}
    for agent_id, fields in v1_document.items():
        if not isinstance(fields, dict) or not isinstance(fields.get("token"), str):
            raise ConfigurationError(f"agent {agent_id!r} is missing a 'token' string")
        agents[agent_id] = _registry_entry(
            tokens.hash_token(fields["token"], key=key),
            enabled=fields.get("enabled", True),
            rate_limit_rps=fields.get("rate_limit_rps"),
        )

    v2_document = {
        "registry_version": tokens.REGISTRY_VERSION,
        "hash_algorithm": tokens.HASH_ALGORITHM,
        "key_id": tokens.derive_key_id(key),
        "agents": agents,
    }
    _atomic_write_json(args.output_path, v2_document)
    print(
        f"wrote {len(agents)} agent(s) to {args.output_path} -- now destroy the "
        f"plaintext original at {args.input_path} (and any backups of it)",
        file=sys.stderr,
    )
    return 0


def _add_key_args(parser: argparse.ArgumentParser) -> None:
    """Add the `--key`/`--key-file` pair shared by every subcommand that needs a deployment key.

    `_resolve_key` enforces that at most one of them is given and that,
    absent both, the ${_AGENT_TOKEN_KEY_ENV} environment variable remains
    the primary path.
    """
    parser.add_argument(
        "--key",
        help=(
            f"Deployment key; defaults to ${_AGENT_TOKEN_KEY_ENV} (not recommended: visible "
            "in ps/shell history/CI logs -- prefer --key-file or the environment variable)"
        ),
    )
    parser.add_argument(
        "--key-file",
        type=Path,
        help="Path to a file containing the deployment key (trailing whitespace stripped)",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="hammertime-agent-token",
        description="Generate, hash, rotate, and migrate ingest agent credentials.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    subparsers.add_parser("new-key", help="Print a fresh deployment key.").set_defaults(
        func=_cmd_new_key
    )

    p_key_id = subparsers.add_parser(
        "key-id", help="Print the fingerprint of the configured deployment key."
    )
    _add_key_args(p_key_id)
    p_key_id.set_defaults(func=_cmd_key_id)

    p_generate = subparsers.add_parser("generate", help="Generate a fresh agent token.")
    p_generate.add_argument("--agent-id", required=True)
    p_generate.add_argument("--rate-limit-rps", type=int, default=None)
    p_generate.add_argument("--disabled", action="store_true")
    p_generate.add_argument("--registry", type=Path, default=None)
    p_generate.add_argument("--replace", action="store_true")
    _add_key_args(p_generate)
    p_generate.set_defaults(func=_cmd_generate)

    p_hash = subparsers.add_parser("hash", help="Hash an externally issued token read from stdin.")
    p_hash.add_argument("--agent-id", required=True)
    p_hash.add_argument("--allow-weak", action="store_true")
    _add_key_args(p_hash)
    p_hash.set_defaults(func=_cmd_hash)

    p_rotate = subparsers.add_parser("rotate", help="Rotate an agent's credential.")
    p_rotate.add_argument("--agent-id", required=True)
    p_rotate.add_argument("--registry", type=Path, required=True)
    p_rotate.add_argument("--overlap-hours", type=float, default=24.0)
    p_rotate.add_argument("--drop-previous", action="store_true")
    _add_key_args(p_rotate)
    p_rotate.set_defaults(func=_cmd_rotate)

    p_migrate = subparsers.add_parser("migrate", help="Convert a v1 plaintext registry to v2.")
    p_migrate.add_argument("--in", dest="input_path", required=True, type=Path)
    p_migrate.add_argument("--out", dest="output_path", required=True, type=Path)
    p_migrate.add_argument(
        "--force",
        action="store_true",
        help="Overwrite --out even if it already exists and is non-empty",
    )
    _add_key_args(p_migrate)
    p_migrate.set_defaults(func=_cmd_migrate)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except HammertimeError as exc:
        print(f"hammertime-agent-token: error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
