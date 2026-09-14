"""Most-specific qualifying prefixes rather than every qualifying ancestor.

Spec: section 31
"""

from __future__ import annotations


# If 10.20.30.0/24 is a bot network, 10.20.0.0/16 will also look elevated.
# Reporting both as separate alerts produces hundreds of nested duplicates, so
# queries may ask for minimal classification only (spec section 31).
