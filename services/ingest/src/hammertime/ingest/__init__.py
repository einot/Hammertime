"""Agent-facing ingestion service.

Spec: section 4 (protocol), section 23 (dedup), section 36 (security).

Responsibilities: terminate TLS, authenticate the agent, validate the payload
against schemas/observation.v1.json, enforce size and rate limits, reject
duplicates, and publish RequestObservation to the log. Nothing here decides
whether an IP is hot -- agents supply observations, never verdicts.
"""
