"""Ingest settings: failed-auth-throttling and observation-budget config keys.

Spec: section 36.5 ("Configuration" block -- five new keys and their
defaults), section 36.6 (`HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS` /
`_BURST` and the `OBSERVATION_BURST >= MAX_OBSERVATIONS` startup
validation). ADR-0007 "Implementation notes" and ADR-0008 "Implementation
notes" name the exact `IngestSettings` field names and defaults used below.

No `test_config.py` predates this file (there is no earlier
`load_settings` test to reconcile against or extend), so this follows
`config.py`'s own `load_settings(env: Mapping[str, str] | None = None)`
convention directly: every test passes an explicit `env` mapping rather
than depending on process environment variables, the same way
`test_pipeline.py`/`test_routes.py` construct an explicit `IngestSettings`
rather than reading `os.environ`.
"""

from __future__ import annotations

import pytest
from hammertime.ingest.config import load_settings


class TestAuthFailureThrottlingDefaults:
    """Section 36.5's "Configuration" block."""

    def test_defaults_when_unset(self) -> None:
        settings = load_settings({})

        assert settings.auth_failure_rate_per_min == 30
        assert settings.auth_failure_burst == 10
        assert settings.auth_failure_agent_rate_per_min == 60
        assert settings.auth_failure_agent_burst == 20
        assert settings.trusted_proxy_hops == 0

    def test_configured_values_are_honoured(self) -> None:
        env = {
            "HAMMERTIME_INGEST_AUTH_FAILURE_RATE_PER_MIN": "15",
            "HAMMERTIME_INGEST_AUTH_FAILURE_BURST": "5",
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_RATE_PER_MIN": "45",
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_BURST": "12",
            "HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS": "2",
        }

        settings = load_settings(env)

        assert settings.auth_failure_rate_per_min == 15
        assert settings.auth_failure_burst == 5
        assert settings.auth_failure_agent_rate_per_min == 45
        assert settings.auth_failure_agent_burst == 12
        assert settings.trusted_proxy_hops == 2

    @pytest.mark.parametrize(
        "key",
        [
            "HAMMERTIME_INGEST_AUTH_FAILURE_RATE_PER_MIN",
            "HAMMERTIME_INGEST_AUTH_FAILURE_BURST",
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_RATE_PER_MIN",
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_BURST",
        ],
    )
    def test_non_numeric_value_is_rejected_at_startup(self, key: str) -> None:
        # Section 36.5: "All five MUST be rejected at startup if non-numeric".
        with pytest.raises(ValueError, match=key):
            load_settings({key: "not-a-number"})

    @pytest.mark.parametrize(
        "key",
        [
            "HAMMERTIME_INGEST_AUTH_FAILURE_RATE_PER_MIN",
            "HAMMERTIME_INGEST_AUTH_FAILURE_BURST",
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_RATE_PER_MIN",
            "HAMMERTIME_INGEST_AUTH_FAILURE_AGENT_BURST",
        ],
    )
    @pytest.mark.parametrize("value", ["0", "-5"])
    def test_non_positive_budget_value_is_rejected_at_startup(self, key: str, value: str) -> None:
        # Section 36.5: "the four budget values if not positive".
        with pytest.raises(ValueError, match=key):
            load_settings({key: value})

    def test_negative_trusted_proxy_hops_is_rejected_at_startup(self) -> None:
        # Section 36.5: "HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS MUST be a
        # non-negative integer."
        with pytest.raises(ValueError, match="HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS"):
            load_settings({"HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS": "-1"})

    def test_non_numeric_trusted_proxy_hops_is_rejected_at_startup(self) -> None:
        with pytest.raises(ValueError, match="HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS"):
            load_settings({"HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS": "not-a-number"})

    def test_trusted_proxy_hops_zero_is_explicitly_valid(self) -> None:
        # 0 is the default and must not be rejected as "falsy".
        settings = load_settings({"HAMMERTIME_INGEST_TRUSTED_PROXY_HOPS": "0"})

        assert settings.trusted_proxy_hops == 0


class TestObservationBudgetConfig:
    """Section 36.6's config block and its startup validation."""

    def test_defaults_when_unset(self) -> None:
        settings = load_settings({})

        assert settings.observation_rate_limit_eps == 2000
        assert settings.observation_burst == 10000

    def test_configured_values_are_honoured(self) -> None:
        env = {
            "HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS": "500",
            "HAMMERTIME_INGEST_OBSERVATION_BURST": "20000",
        }

        settings = load_settings(env)

        assert settings.observation_rate_limit_eps == 500
        assert settings.observation_burst == 20000

    def test_observation_burst_below_max_observations_is_rejected_at_startup(self) -> None:
        # Section 36.6: "HAMMERTIME_INGEST_OBSERVATION_BURST MUST be
        # greater than or equal to HAMMERTIME_INGEST_MAX_OBSERVATIONS, and
        # the service MUST refuse to start otherwise."
        env = {
            "HAMMERTIME_INGEST_MAX_OBSERVATIONS": "10000",
            "HAMMERTIME_INGEST_OBSERVATION_BURST": "9999",
        }

        with pytest.raises(ValueError, match="OBSERVATION_BURST"):
            load_settings(env)

    def test_the_rejection_names_both_keys(self) -> None:
        # ADR-0008 implementation notes: "naming both keys and both values".
        env = {
            "HAMMERTIME_INGEST_MAX_OBSERVATIONS": "10000",
            "HAMMERTIME_INGEST_OBSERVATION_BURST": "1",
        }

        with pytest.raises(ValueError) as excinfo:
            load_settings(env)

        message = str(excinfo.value)
        assert "OBSERVATION_BURST" in message
        assert "MAX_OBSERVATIONS" in message

    def test_observation_burst_equal_to_max_observations_is_accepted(self) -> None:
        # The boundary itself ("greater than or equal to") must not be
        # rejected -- only strictly-below is invalid.
        env = {
            "HAMMERTIME_INGEST_MAX_OBSERVATIONS": "10000",
            "HAMMERTIME_INGEST_OBSERVATION_BURST": "10000",
        }

        settings = load_settings(env)

        assert settings.observation_burst == settings.max_observations == 10000

    def test_observation_burst_above_max_observations_is_accepted(self) -> None:
        env = {
            "HAMMERTIME_INGEST_MAX_OBSERVATIONS": "500",
            "HAMMERTIME_INGEST_OBSERVATION_BURST": "10000",
        }

        settings = load_settings(env)

        assert settings.observation_burst == 10000
        assert settings.max_observations == 500

    def test_default_observation_burst_satisfies_default_max_observations(self) -> None:
        # Regression guard: the documented defaults (10000 / 10000) must
        # themselves pass load_settings's own validation -- a service that
        # cannot start with its own advertised defaults would be a bug.
        settings = load_settings({})

        assert settings.observation_burst >= settings.max_observations

    @pytest.mark.parametrize(
        "key",
        [
            "HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS",
            "HAMMERTIME_INGEST_OBSERVATION_BURST",
        ],
    )
    def test_non_numeric_value_is_rejected_at_startup(self, key: str) -> None:
        with pytest.raises(ValueError, match=key):
            load_settings({key: "not-a-number"})

    @pytest.mark.parametrize(
        "key",
        [
            "HAMMERTIME_INGEST_OBSERVATION_RATE_LIMIT_EPS",
            "HAMMERTIME_INGEST_OBSERVATION_BURST",
        ],
    )
    def test_non_positive_value_is_rejected_at_startup(self, key: str) -> None:
        with pytest.raises(ValueError, match=key):
            load_settings({key: "0"})
