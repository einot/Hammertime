"""DetectionConfig validation and the config loader (spec section 34)."""

from __future__ import annotations

import itertools
import json
from pathlib import Path
from typing import Any

import pytest
from hammertime.core.config.loader import load, watch
from hammertime.core.config.models import DetectionConfig
from hammertime.core.errors import ConfigurationError

# Loosely typed on purpose: several tests below deliberately build malformed
# variants (wrong type, extra key) to exercise loader.load's *runtime*
# rejection of them, which a precise static type (e.g. a TypedDict) would
# instead reject at type-check time.
VALID_DOC: dict[str, Any] = {
    "config_version": 1,
    "window_seconds": 300,
    "bucket_seconds": 10,
    "hot_threshold": 1000,
    "cold_threshold": 800,
    "minimum_hot_ips": 16,
    "minimum_hot_ratio": 0.10,
    "allowed_lateness_seconds": 30,
    "state_retention_seconds": 600,
}


class TestDetectionConfigDefaults:
    def test_defaults_are_internally_consistent(self) -> None:
        DetectionConfig()  # must not raise

    def test_bucket_count(self) -> None:
        assert DetectionConfig(window_seconds=300, bucket_seconds=10).bucket_count == 30


class TestDetectionConfigValidation:
    def test_rejects_cold_threshold_at_hot_threshold(self) -> None:
        with pytest.raises(ConfigurationError):
            DetectionConfig(hot_threshold=100, cold_threshold=100)

    def test_rejects_cold_threshold_above_hot_threshold(self) -> None:
        with pytest.raises(ConfigurationError):
            DetectionConfig(hot_threshold=100, cold_threshold=200)

    def test_rejects_window_not_a_multiple_of_bucket(self) -> None:
        with pytest.raises(ConfigurationError):
            DetectionConfig(window_seconds=305, bucket_seconds=10)

    def test_rejects_retention_shorter_than_window(self) -> None:
        with pytest.raises(ConfigurationError):
            DetectionConfig(window_seconds=300, state_retention_seconds=100)

    @pytest.mark.parametrize("ratio", [-0.1, 1.1])
    def test_rejects_hot_ratio_outside_unit_interval(self, ratio: float) -> None:
        with pytest.raises(ConfigurationError):
            DetectionConfig(minimum_hot_ratio=ratio)

    @pytest.mark.parametrize("ratio", [0.0, 1.0])
    def test_accepts_hot_ratio_at_unit_interval_bounds(self, ratio: float) -> None:
        DetectionConfig(minimum_hot_ratio=ratio)  # must not raise

    @pytest.mark.parametrize("version", [0, -1])
    def test_rejects_invalid_config_version(self, version: int) -> None:
        with pytest.raises(ConfigurationError):
            DetectionConfig(config_version=version)


class TestWeightFunctionAndWeightMax:
    """spec section 46.4, ADR-0005: weight_function/weight_max drive
    IpAttributes.weight but are validated as ordinary DetectionConfig fields
    (schemas/detection_config.v1.json)."""

    def test_defaults_match_the_schema_defaults(self) -> None:
        config = DetectionConfig()
        assert config.weight_function == "threshold_ratio"
        assert config.weight_max == 1000000

    def test_accepts_the_only_registered_weight_function(self) -> None:
        DetectionConfig(weight_function="threshold_ratio")  # must not raise

    @pytest.mark.parametrize(
        "weight_function", ["", "THRESHOLD_RATIO", "made_up_function", "linear"]
    )
    def test_rejects_unrecognized_weight_function(self, weight_function: str) -> None:
        with pytest.raises(ConfigurationError):
            DetectionConfig(weight_function=weight_function)

    @pytest.mark.parametrize("weight_max", [1000, 1000000])
    def test_accepts_weight_max_at_schema_bounds(self, weight_max: int) -> None:
        DetectionConfig(weight_max=weight_max)  # must not raise

    @pytest.mark.parametrize("weight_max", [999, 1000001])
    def test_rejects_weight_max_outside_schema_bounds(self, weight_max: int) -> None:
        with pytest.raises(ConfigurationError):
            DetectionConfig(weight_max=weight_max)


def _write(path: Path, doc: dict[str, Any]) -> Path:
    config_path = path / "detection.json"
    config_path.write_text(json.dumps(doc))
    return config_path


class TestLoad:
    def test_loads_the_repo_sample_config(self) -> None:
        sample = Path(__file__).parents[6] / "config" / "detection.v1.json"
        config = load(sample)
        assert config == DetectionConfig()

    def test_repo_sample_config_declares_weight_function_and_weight_max(self) -> None:
        # ADR-0005 consequences: "schemas/detection_config.v1.json gains
        # weight_function and weight_max ahead of the code that reads them
        # ... Closing that gap is the first task of the implementing issue."
        # The on-disk default config must therefore be updated to actually
        # declare these keys, not merely fall back to DetectionConfig's
        # in-code defaults.
        sample = Path(__file__).parents[6] / "config" / "detection.v1.json"
        raw = json.loads(sample.read_text())
        assert raw["weight_function"] == "threshold_ratio"
        assert raw["weight_max"] == 1000000

    def test_loads_the_repo_sample_config_with_weight_fields(self) -> None:
        sample = Path(__file__).parents[6] / "config" / "detection.v1.json"
        config = load(sample)
        assert config.weight_function == "threshold_ratio"
        assert config.weight_max == 1000000

    def test_happy_path(self, tmp_path: Path) -> None:
        config = load(_write(tmp_path, VALID_DOC))
        assert config == DetectionConfig(**VALID_DOC)

    def test_missing_file(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError):
            load(tmp_path / "does-not-exist.json")

    def test_invalid_json(self, tmp_path: Path) -> None:
        path = tmp_path / "detection.json"
        path.write_text("{not json")
        with pytest.raises(ConfigurationError):
            load(path)

    def test_document_must_be_an_object(self, tmp_path: Path) -> None:
        path = tmp_path / "detection.json"
        path.write_text("[1, 2, 3]")
        with pytest.raises(ConfigurationError):
            load(path)

    def test_missing_required_key(self, tmp_path: Path) -> None:
        doc = dict(VALID_DOC)
        del doc["hot_threshold"]
        with pytest.raises(ConfigurationError, match="hot_threshold"):
            load(_write(tmp_path, doc))

    def test_unknown_key_rejected(self, tmp_path: Path) -> None:
        doc = dict(VALID_DOC, unexpected_field=123)
        with pytest.raises(ConfigurationError, match="unexpected_field"):
            load(_write(tmp_path, doc))

    def test_non_integer_where_integer_required(self, tmp_path: Path) -> None:
        doc = dict(VALID_DOC, hot_threshold=1000.5)
        with pytest.raises(ConfigurationError, match="hot_threshold"):
            load(_write(tmp_path, doc))

    def test_bool_is_not_accepted_as_integer(self, tmp_path: Path) -> None:
        doc = dict(VALID_DOC, hot_threshold=True)
        with pytest.raises(ConfigurationError, match="hot_threshold"):
            load(_write(tmp_path, doc))

    def test_invalid_config_version_surfaces_as_configuration_error(self, tmp_path: Path) -> None:
        doc = dict(VALID_DOC, config_version=0)
        with pytest.raises(ConfigurationError):
            load(_write(tmp_path, doc))

    def test_bad_threshold_ordering_surfaces_as_configuration_error(self, tmp_path: Path) -> None:
        doc = dict(VALID_DOC, hot_threshold=100, cold_threshold=200)
        with pytest.raises(ConfigurationError):
            load(_write(tmp_path, doc))

    def test_happy_path_with_weight_fields(self, tmp_path: Path) -> None:
        # Regression guard for the specific gotcha ADR-0005 calls out: a
        # loader that classifies every known key as an integer field (as
        # `_INTEGER_KEYS` does today by deriving from all dataclass fields
        # minus `minimum_hot_ratio`) would wrongly reject the string value
        # "threshold_ratio" for weight_function.
        doc = dict(VALID_DOC, weight_function="threshold_ratio", weight_max=500000)
        config = load(_write(tmp_path, doc))
        assert config.weight_function == "threshold_ratio"
        assert config.weight_max == 500000

    def test_unrecognized_weight_function_surfaces_as_configuration_error(
        self, tmp_path: Path
    ) -> None:
        doc = dict(VALID_DOC, weight_function="made_up_function")
        with pytest.raises(ConfigurationError):
            load(_write(tmp_path, doc))

    @pytest.mark.parametrize("weight_max", [1000, 1000000])
    def test_loader_accepts_weight_max_at_schema_bounds(
        self, tmp_path: Path, weight_max: int
    ) -> None:
        doc = dict(VALID_DOC, weight_max=weight_max)
        config = load(_write(tmp_path, doc))
        assert config.weight_max == weight_max

    @pytest.mark.parametrize("weight_max", [999, 1000001])
    def test_loader_rejects_weight_max_outside_schema_bounds(
        self, tmp_path: Path, weight_max: int
    ) -> None:
        doc = dict(VALID_DOC, weight_max=weight_max)
        with pytest.raises(ConfigurationError):
            load(_write(tmp_path, doc))


class TestWatch:
    def test_yields_initial_config_then_a_change(self, tmp_path: Path) -> None:
        path = _write(tmp_path, VALID_DOC)
        sleeps: list[float] = []

        def fake_sleep(seconds: float) -> None:
            sleeps.append(seconds)
            if len(sleeps) == 1:
                _write(tmp_path, dict(VALID_DOC, hot_threshold=2000))

        seen = list(itertools.islice(watch(path, poll_interval_seconds=0, sleep=fake_sleep), 2))

        assert seen[0] == DetectionConfig(**VALID_DOC)
        assert seen[1] == DetectionConfig(**dict(VALID_DOC, hot_threshold=2000))

    def test_does_not_yield_again_when_content_is_unchanged(self, tmp_path: Path) -> None:
        path = _write(tmp_path, VALID_DOC)
        calls = 0

        class _StopWatching(Exception):
            """Escape hatch distinct from StopIteration, which PEP 479 turns
            into a RuntimeError if raised inside the generator under test."""

        def fake_sleep(seconds: float) -> None:
            nonlocal calls
            calls += 1
            if calls >= 5:
                raise _StopWatching

        with pytest.raises(_StopWatching):
            list(watch(path, poll_interval_seconds=0, sleep=fake_sleep))
