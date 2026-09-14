"""Controlled re-evaluation after a configuration version change.

Spec: section 34
"""

from __future__ import annotations


# Lowering hot_threshold mass-transitions COLD -> HOT. The job walks live IP
# state, re-runs evaluate_ip_state under the new config, and emits the resulting
# transitions at a bounded rate, tagged with the new config_version, instead of
# leaving stale state behind (spec section 34).
