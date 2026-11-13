"""Deterministic identifiers.

Every downstream write is idempotent on a natural key (docs/02 §8). That is
only true if the key is a pure function of the thing it names, so all ids
here are content-derived. Re-running the pipeline over the same events
produces the same ids and therefore the same rows.
"""

from __future__ import annotations

import hashlib
import uuid
from datetime import datetime

_NAMESPACE = uuid.UUID("6d3c1f2e-9a4b-5c7d-8e1f-0a2b3c4d5e6f")


def _digest(*parts: object) -> str:
    joined = "|".join("" if p is None else str(p) for p in parts)
    return hashlib.sha256(joined.encode("utf-8")).hexdigest()


def stable_uuid(*parts: object) -> str:
    """A UUIDv5 over the given parts — readable in Postgres, stable across runs."""
    joined = "|".join("" if p is None else str(p) for p in parts)
    return str(uuid.uuid5(_NAMESPACE, joined))


def profile_id(table_id: str, column: str | None, window_end: datetime, version: str) -> str:
    return "pf_" + _digest(table_id, column, window_end.isoformat(), version)[:24]


def anomaly_id(profile: str, detector: str) -> str:
    return "an_" + _digest(profile, detector)[:24]


def incident_id(root_asset: str, root_column: str | None, window_key: str) -> str:
    return stable_uuid("incident", root_asset, root_column, window_key)


def evidence_id(incident: str, tool: str, ref: str) -> str:
    """Short, human-quotable id — these appear in agent prose and in the UI."""
    return "ev_" + _digest(incident, tool, ref)[:8]


def action_id(incident: str, action_type: str, params_key: str) -> str:
    return stable_uuid("action", incident, action_type, params_key)


def salted_hash(value: object, salt: str) -> str:
    """Used for `min_value_hash` / `max_value_hash`.

    The salt is what stops the hash being a lookup oracle for low-cardinality
    columns. We keep 16 hex chars — enough to detect change, useless for
    recovering the value (ADR-010).
    """
    return _digest(salt, value)[:16]
