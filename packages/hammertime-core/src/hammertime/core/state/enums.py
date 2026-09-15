"""State vocabulary.

Spec: section 6 (IP state), section 9 (prefix_state).
"""

from __future__ import annotations

from enum import StrEnum


class IpState(StrEnum):
    COLD = "COLD"
    HOT = "HOT"


class PrefixState(StrEnum):
    NORMAL = "NORMAL"
    HOT_PREFIX = "HOT_PREFIX"
    BOT_NETWORK = "BOT_NETWORK"
