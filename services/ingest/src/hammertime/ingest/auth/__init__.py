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
    """No registered agent matches the `agent_id` presented, or none was
    presented at all (missing `X-Agent-Id`/`Authorization` header).

    Maps to HTTP 401 -- docs/protocol/observation-v1.md's response table
    ("401 / 403 | Unknown or unauthorized agent").
    """


class AuthorizationError(AgentAuthError):
    """Base for every failure that maps to HTTP 403: known agent, denied.

    See `AuthenticationError` for the 401/403 split this mirrors.
    """


class InvalidCredentialError(AuthorizationError):
    """A known agent's bearer token did not match its registered credential.

    Maps to HTTP 403, distinct from `UnknownAgentError`'s 401: the agent
    exists, but the credential presented for it is wrong.
    """


class AgentDisabledError(AuthorizationError):
    """A known agent exists in the registry but is administratively disabled.

    Maps to HTTP 403, same as `InvalidCredentialError`: the agent is known,
    but not currently authorized to submit observations.
    """
