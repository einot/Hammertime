"""Read API: GET /prefix/{cidr}, GET /ip/{addr}, GET /prefixes/hot.

Spec: section 29, section 31
"""

from __future__ import annotations


# /prefix/192.168.0.0/16 -> hot_ips, capacity, hot_ratio, state
# /ip/192.168.1.42       -> state, request_count, matched_prefixes along the path
# /prefixes/hot?minimal=true -> most specific qualifying prefixes only (section 31)
