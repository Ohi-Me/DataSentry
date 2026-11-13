"""The event backbone.

Production runs Kafka with a Schema Registry and Avro; the topic names,
partition keys and retentions in ``TOPICS`` below are the same ones declared
in docs/03 section 1. Locally the same log is materialised as an append-only
table so the whole system replays without a broker.

What this preserves from the real thing, and why it matters:

* **Ordering per key.** Offsets are monotonic and consumers read in offset
  order, so per-``table_id`` profile ordering and per-``incident_id``
  lifecycle ordering hold exactly as they do with key-based partitioning.
* **Replay.** Nothing is consumed destructively. A consumer is a topic plus
  an offset, so re-running a new detector over last week's profiles is
  ``consume(topic, after=0)`` — which is what makes the backtest gate in CI
  possible at all.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from datasentry_core.clock import SYSTEM_CLOCK, Clock
from datasentry_core.store import ControlPlane, control_plane


@dataclass(frozen=True)
class TopicSpec:
    name: str
    key: str
    partitions: int
    retention_days: int
    producer: str
    consumers: tuple[str, ...]


TOPICS: dict[str, TopicSpec] = {
    t.name: t
    for t in [
        TopicSpec("ds.warehouse.write.v1", "table_id", 12, 7, "ingestion-gateway", ("profiler",)),
        TopicSpec("ds.query.executed.v1", "warehouse_id", 6, 7, "ingestion-gateway", ("lineage-builder",)),
        TopicSpec("ds.job.run.v1", "job_id", 6, 30, "ingestion-gateway", ("profiler", "orchestrator")),
        TopicSpec("ds.git.event.v1", "repo", 3, 90, "ingestion-gateway", ("lineage-builder", "orchestrator")),
        TopicSpec("ds.schema.changed.v1", "table_id", 6, 90, "ingestion-gateway", ("lineage-builder", "detector")),
        TopicSpec("ds.profile.computed.v1", "table_id", 12, 7, "profiler", ("detector",)),
        TopicSpec("ds.anomaly.detected.v1", "table_id", 12, 30, "detector", ("correlator",)),
        TopicSpec("ds.incident.created.v1", "incident_id", 6, 90, "correlator", ("orchestrator", "api")),
        TopicSpec("ds.incident.updated.v1", "incident_id", 6, 90, "orchestrator", ("api",)),
        TopicSpec("ds.action.requested.v1", "incident_id", 3, 90, "policy-gate", ("action-executor",)),
        TopicSpec("ds.action.completed.v1", "incident_id", 3, 90, "action-executor", ("verification", "api")),
    ]
}

WAREHOUSE_WRITE = "ds.warehouse.write.v1"
QUERY_EXECUTED = "ds.query.executed.v1"
JOB_RUN = "ds.job.run.v1"
GIT_EVENT = "ds.git.event.v1"
SCHEMA_CHANGED = "ds.schema.changed.v1"
PROFILE_COMPUTED = "ds.profile.computed.v1"
ANOMALY_DETECTED = "ds.anomaly.detected.v1"
INCIDENT_CREATED = "ds.incident.created.v1"
INCIDENT_UPDATED = "ds.incident.updated.v1"
ACTION_REQUESTED = "ds.action.requested.v1"
ACTION_COMPLETED = "ds.action.completed.v1"


class EventBus:
    def __init__(self, store: ControlPlane | None = None, clock: Clock | None = None) -> None:
        self.store = store or control_plane()
        self.clock = clock or SYSTEM_CLOCK

    def publish(self, topic: str, key: str, payload: dict[str, Any],
                at: datetime | None = None) -> int:
        if topic not in TOPICS:
            raise KeyError("unknown topic: " + topic)
        return self.store.publish(topic, key, payload, at or self.clock.now())

    def consume(self, topic: str, *, after: int = 0, limit: int = 1000) -> list[dict[str, Any]]:
        return self.store.consume(topic, after=after, limit=limit)

    def lag(self, topic: str, committed: int) -> int:
        latest = self.store.scalar(
            "SELECT MAX(offset_id) FROM events WHERE topic = ?", (topic,), default=0
        )
        return max(0, int(latest) - committed)
