"""The verification worker: did the fix actually work?

This is what makes the loop closed rather than a notification pipeline. After
a remediation lands, the affected asset is re-profiled and the anomaly has to
*clear*. If it has not cleared within two cycles, the incident escalates
rather than auto-closing.

That asymmetry is deliberate. Auto-closing on a timer is how a monitoring
system develops a reputation for lying: the incident disappears, the data is
still wrong, and the next person to notice is a stakeholder. An escalation is
noisier and correct.

The verification evidence itself is stored on the incident, so "resolved"
carries the measurement that justified it rather than a status word.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from typing import Any

from datasentry_core.clock import SYSTEM_CLOCK, Clock, iso
from datasentry_core.enums import EvidenceKind, IncidentStatus
from datasentry_core.ids import evidence_id
from datasentry_core.logging import get_logger
from datasentry_core.models import Evidence, Incident

log = get_logger("verification")

REQUIRED_CLEAN_CYCLES = 2


@dataclass
class VerificationOutcome:
    incident_id: str
    cleared: bool
    checked_cycles: int
    still_breaching: list[str] = field(default_factory=list)
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def status(self) -> IncidentStatus:
        return IncidentStatus.RESOLVED if self.cleared else IncidentStatus.ESCALATED


class VerificationWorker:
    def __init__(self, store: Any, *, clock: Clock | None = None,
                 identity: str = "service:datasentry-verification") -> None:
        self.store = store
        self.clock = clock or SYSTEM_CLOCK
        self.identity = identity

    def verify(self, incident: Incident, *, since: datetime | None = None,
               ensemble: Any = None, contexts: Any = None) -> VerificationOutcome:
        """Re-score the affected series and decide: close or escalate."""
        now = self.clock.now()
        since = since or (incident.rca_completed_at or incident.first_detected_at)

        anomalies = self.store.anomalies_for_incident(incident.incident_id)
        series = {(a.asset_id, a.column_name) for a in anomalies}
        if not series:
            series = {(incident.root_asset_id, incident.root_column)}

        still: list[str] = []
        checked = 0
        measurements: list[dict[str, Any]] = []

        for asset_id, column in sorted(series, key=lambda s: (s[0], s[1] or "")):
            recent = self.store.profile_series(asset_id, column, limit=REQUIRED_CLEAN_CYCLES + 1)
            post_fix = [p for p in recent if p.window_end > since]
            checked = max(checked, len(post_fix))
            if len(post_fix) < REQUIRED_CLEAN_CYCLES:
                # Not enough evidence yet. Neither closed nor escalated: the
                # worker will be scheduled again.
                continue

            fresh_anomalies = [
                a for a in self.store.anomalies_for_asset(asset_id, limit=20)
                if a.detected_at > since and a.column_name == column
            ]
            label = asset_id.split(".")[-1] + ("." + column if column else "")
            if fresh_anomalies:
                still.append(label)
                measurements.append({
                    "series": label, "cleared": False,
                    "signals": sorted({str(a.signal) for a in fresh_anomalies}),
                    "cycles_checked": len(post_fix),
                })
            else:
                measurements.append({
                    "series": label, "cleared": True,
                    "cycles_checked": len(post_fix),
                    "latest_rows": post_fix[-1].row_count,
                    "latest_null_rate": (
                        round(post_fix[-1].null_rate, 6)
                        if post_fix[-1].null_rate is not None else None
                    ),
                })

        enough = checked >= REQUIRED_CLEAN_CYCLES
        cleared = enough and not still

        outcome = VerificationOutcome(
            incident_id=incident.incident_id,
            cleared=cleared,
            checked_cycles=checked,
            still_breaching=still,
            detail={
                "since": iso(since),
                "required_clean_cycles": REQUIRED_CLEAN_CYCLES,
                "series_checked": len(series),
                "measurements": measurements,
                "sufficient_evidence": enough,
            },
        )

        if not enough:
            log.event("verification deferred", incident=incident.incident_id[:8],
                      cycles=checked, need=REQUIRED_CLEAN_CYCLES)
            return outcome

        self._record(incident, outcome, now)
        return outcome

    def _record(self, incident: Incident, outcome: VerificationOutcome,
                now: datetime) -> None:
        summary = (
            "anomaly cleared across " + str(outcome.checked_cycles)
            + " post-remediation cycles"
            if outcome.cleared
            else "anomaly persists on " + ", ".join(outcome.still_breaching[:3])
            + " after " + str(outcome.checked_cycles) + " cycles"
        )
        self.store.insert_evidence([
            Evidence(
                evidence_id=evidence_id(incident.incident_id, "verification", iso(now)),
                incident_id=incident.incident_id,
                kind=EvidenceKind.PROFILE_DELTA,
                ref="verification@" + iso(now),
                summary=summary,
                payload=outcome.detail,
                collected_by="verification_worker",
                collected_at=now,
            )
        ])

        status = outcome.status
        self.store.set_incident_status(
            incident.incident_id, status,
            resolved_at=now if outcome.cleared else None,
        )
        self.store.audit(
            actor=self.identity, actor_kind="service",
            action="incident.resolved" if outcome.cleared else "incident.escalated",
            target=incident.incident_id,
            detail={"cleared": outcome.cleared, "still_breaching": outcome.still_breaching,
                    "cycles": outcome.checked_cycles},
            at=now,
        )
        log.event(
            "verification complete" if outcome.cleared else "verification escalated",
            incident=incident.incident_id[:8], cleared=outcome.cleared,
            breaching=len(outcome.still_breaching),
        )

    def due(self, *, grace_minutes: int = 30) -> list[Incident]:
        """Incidents whose remediation has had time to take effect."""
        cutoff = self.clock.now() - timedelta(minutes=grace_minutes)
        out: list[Incident] = []
        for incident in self.store.list_incidents(status=str(IncidentStatus.VERIFYING)):
            if incident.rca_completed_at and incident.rca_completed_at <= cutoff:
                out.append(incident)
        return out
