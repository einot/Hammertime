"""Provision the JetStream streams every registered topic needs, before any service starts.

Spec: section 19, section 32, section 33

ADR-0013 decision 2: JetStream does not create streams on publish, so
`hammertime-provision` is the one place a stream is ever created or
reconciled. The services only verify that their streams exist.
"""
