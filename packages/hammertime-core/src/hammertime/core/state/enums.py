"""State vocabulary.

Spec: section 6 (IP state), section 9 (prefix_state).
"""

from __future__ import annotations

from enum import Enum


class IpState(str, Enum):
    COLD = "COLD"
    HOT = "HOT"


class PrefixState(str, Enum):
    NORMAL = "NORMAL"
    HOT_PREFIX = "HOT_PREFIX"
    BOT_NETWORK = "BOT_NETWORK"
