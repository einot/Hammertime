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


class UnknownAgentError(AgentAuthError):
    """No registered agent matches the `agent_id` presented.

    Maps to HTTP 401 -- docs/protocol/observation-v1.md's response table
    ("401 / 403 | Unknown or unauthorized agent").
    """


class InvalidCredentialError(AgentAuthError):
    """A known agent's bearer token did not match its registered credential.

    Maps to HTTP 403, distinct from `UnknownAgentError`'s 401: the agent
    exists, but the credential presented for it is wrong.
    """


class AgentDisabledError(AgentAuthError):
    """A known agent exists in the registry but is administratively disabled.

    Maps to HTTP 403, same as `InvalidCredentialError`: the agent is known,
    but not currently authorized to submit observations.
    """
