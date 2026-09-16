"""Agent identity and authorization (spec section 36).

Exception hierarchy for `hammertime.ingest.auth.agents` and
`hammertime.ingest.auth.middleware`, mirroring how
`hammertime.ingest.validation` roots its own errors in
`hammertime.core.errors.HammertimeError`.
"""

from hammertime.core.errors import HammertimeError


class AgentAuthError(HammertimeError):
    """Base class for ingest agent authentication/authorization failures.

    Spec: section 36.
    """


class AuthenticationError(AgentAuthError):
    """Base for every failure that maps to HTTP 401: no valid identity claim.

    Groups everything meaning "this request never established who the
    caller is" -- as opposed to `AuthorizationError`, "we know who you are
    and you're not permitted" -- mirroring how `hammertime.ingest.validation`
    groups its own 413-mapping errors under `RequestLimitExceededError`, so
    a future request handler can catch one base class per status code.
    """


class UnknownAgentError(AuthenticationError):
    """No registered agent matches the `agent_id` presented.

    Maps to HTTP 401, with the uniform body and logging `outcome` ADR-0007 /
    spec section 36.5 require: this is indistinguishable on the wire from
    every other authentication failure, including `InvalidCredentialError`
    below (which stays in the `AuthorizationError`/403 *exception* group,
    since the agent genuinely is known -- only the HTTP mapping collapses).
    """


class MissingCredentialError(AuthenticationError):
    """No identity claim was presented at all: missing `X-Agent-Id`, missing
    or malformed `Authorization: Bearer <token>`, or an empty token.

    Maps to HTTP 401 (spec section 36.5's `missing_credential` outcome).
    """


class OversizedCredentialError(AuthenticationError):
    """An `X-Agent-Id` over 128 characters or a bearer token over 512
    characters was presented, rejected before any hashing/comparison work
    (spec section 36.5's `oversized_credential` outcome, a cheap DoS guard,
    not token-strength enforcement).

    Maps to HTTP 401.
    """


class AuthorizationError(AgentAuthError):
    """Base for every failure where a known agent's identity is established.

    Historically this whole group mapped to HTTP 403; ADR-0007 (spec
    section 36.5) narrows that to `AgentDisabledError` alone --
    `InvalidCredentialError` now maps to the same uniform 401 as every
    other authentication failure, so that a wrong-token guess against a
    real `agent_id` is not distinguishable from a guess against a
    nonexistent one. The exception hierarchy itself is unchanged (both
    errors mean "we know who you claim to be"); only the HTTP status
    `auth/middleware.py` picks for each has changed.
    """


class InvalidCredentialError(AuthorizationError):
    """A known agent's bearer token did not match its registered credential.

    Maps to HTTP 401 (ADR-0007's uniform failure response, spec section
    36.5) -- see `AuthorizationError` above for why this no longer maps to
    403 despite remaining in the "known agent" exception group.
    """


class AgentDisabledError(AuthorizationError):
    """A known agent exists in the registry but is administratively disabled.

    Maps to HTTP 403 -- the one case ADR-0007 (spec section 36.5) keeps
    distinguishable, since learning it requires already holding the
    agent's live token.
    """
