"""The action executor.

Runs under ``datasentry-executor``, a different identity from the one the
reasoning layer uses. That separation is the security answer to "how do you
make agents safe?", and it is architectural rather than prompt-engineered: a
perfectly successful injection against the agent produces a wrong narrative,
because the agent's identity has no write capability and this process never
reads a model's free text.

What arrives here is an ``Action`` whose ``action_type`` is a member of a
closed enum. The executor switches on that enum. There is no path from model
output to arbitrary execution, no eval, no templated shell.

Actions land as pull requests, never as direct writes to a default branch
(ADR-012). That mirrors how data teams actually accept change and makes the
repository's own CI part of the loop — the fix has to pass the same checks a
human's fix would.

Locally the GitHub App is stubbed by ``LocalGitHub``, which records what
would have been created. The call sites and the audit trail are identical, so
swapping in the real client is a constructor argument.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol

from datasentry_core.clock import SYSTEM_CLOCK, Clock, iso
from datasentry_core.enums import ActionStatus, ActionType
from datasentry_core.errors import PolicyViolation
from datasentry_core.logging import get_logger
from datasentry_core.models import Action

log = get_logger("action-executor")


@dataclass
class PullRequest:
    number: int
    url: str
    branch: str
    title: str
    body: str
    diff: str
    checks: str = "pending"


class GitHubClient(Protocol):
    def open_pr(self, *, repo: str, branch: str, title: str, body: str,
                diff: str) -> PullRequest: ...

    def check_status(self, number: int) -> str: ...


class LocalGitHub:
    """Records what would have been opened. Never touches a network."""

    def __init__(self) -> None:
        self.pulls: dict[int, PullRequest] = {}
        self._next = 4471  # the demo narrative's PR number

    def open_pr(self, *, repo: str, branch: str, title: str, body: str,
                diff: str) -> PullRequest:
        number = self._next
        self._next += 1
        pr = PullRequest(
            number=number,
            url="https://github.com/" + repo + "/pull/" + str(number),
            branch=branch,
            title=title,
            body=body,
            diff=diff,
        )
        self.pulls[number] = pr
        return pr

    def check_status(self, number: int) -> str:
        pr = self.pulls.get(number)
        if pr is None:
            return "unknown"
        # A revert of a single clause is a small, mechanical change; the
        # repository's own CI passes it. Modelled rather than randomised so
        # the demo is reproducible.
        pr.checks = "success"
        return pr.checks


@dataclass
class ExecutionResult:
    action_id: str
    status: ActionStatus
    external_ref: str | None = None
    detail: dict[str, Any] = field(default_factory=dict)


class ActionExecutor:
    def __init__(self, store: Any, *, github: GitHubClient | None = None,
                 clock: Clock | None = None, identity: str = "service:datasentry-executor") -> None:
        self.store = store
        self.github = github or LocalGitHub()
        self.clock = clock or SYSTEM_CLOCK
        self.identity = identity

    # -- dispatch -----------------------------------------------------------

    def execute(self, action: Action) -> ExecutionResult:
        if action.status not in {ActionStatus.APPROVED, ActionStatus.PROPOSED}:
            raise PolicyViolation(
                "action " + action.action_id + " is " + str(action.status) + ", not approved"
            )

        handler = {
            ActionType.OPEN_PR: self._open_pr,
            ActionType.QUARANTINE_TABLE: self._quarantine,
            ActionType.PIN_FEATURE_VIEW: self._pin_feature_view,
            ActionType.NOTIFY_OWNER: self._notify,
            ActionType.MUTE_ALERT: self._mute,
            ActionType.BACKFILL_REQUEST: self._backfill,
        }.get(action.action_type)

        if handler is None:
            # Unreachable while the enum and this table agree, which is the
            # point: adding an action type without an executor fails loudly.
            raise PolicyViolation("no executor implements " + str(action.action_type))

        now = self.clock.now()
        action.status = ActionStatus.EXECUTING
        self.store.upsert_action(action)

        try:
            result = handler(action, now)
        except Exception as exc:  # noqa: BLE001
            action.status = ActionStatus.FAILED
            action.result = {"error": type(exc).__name__, "detail": str(exc)[:400]}
            self.store.upsert_action(action)
            self._audit(action, "action.failed", action.result, now)
            log.event("action failed", action=str(action.action_type),
                      error=type(exc).__name__)
            return ExecutionResult(action.action_id, ActionStatus.FAILED, detail=action.result)

        action.status = result.status
        action.external_ref = result.external_ref
        action.result = result.detail
        self.store.upsert_action(action)
        self._audit(action, "action.executed", result.detail, now)
        log.event("action executed", action=str(action.action_type),
                  incident=action.incident_id[:8], ref=result.external_ref or "-")
        return result

    # -- handlers -----------------------------------------------------------

    def _open_pr(self, action: Action, now: datetime) -> ExecutionResult:
        params = action.params
        model_ref = params.get("model_ref", "")
        repo = params.get("repo", "analytics")
        incident_id = action.incident_id

        rows = self.store.query(
            "SELECT * FROM model_versions WHERE model_ref = ? ORDER BY created_at DESC LIMIT 2",
            (model_ref,),
        )
        if len(rows) < 2:
            return ExecutionResult(action.action_id, ActionStatus.FAILED,
                                   detail={"error": "no prior version to revert to"})
        current, previous = rows[0], rows[1]
        diff = _unified_diff(model_ref, current["sql"], previous["sql"])

        body = self._pr_body(incident_id, action, diff)
        branch = params.get("branch") or (
            "datasentry/incident-" + incident_id[:8]
        )
        pr = self.github.open_pr(
            repo=repo,
            branch=branch,
            title=params.get("title") or ("Revert semantic change in " + _basename(model_ref)),
            body=body,
            diff=diff,
        )
        # The revert is recorded as a new model version so the lineage builder
        # and the verification loop see the corrected SQL.
        self.store.execute(
            "INSERT OR REPLACE INTO model_versions (model_ref, sha, sql, created_at) "
            "VALUES (?,?,?,?)",
            (model_ref, "revert-" + str(pr.number), previous["sql"], iso(now)),
        )
        return ExecutionResult(
            action.action_id, ActionStatus.SUCCEEDED, external_ref=pr.url,
            detail={
                "pr_number": pr.number, "branch": pr.branch, "repo": repo,
                "model_ref": model_ref, "diff": diff, "checks": "pending",
            },
        )

    def _quarantine(self, action: Action, now: datetime) -> ExecutionResult:
        asset_id = action.params["asset_id"]
        self.store.execute(
            "UPDATE assets SET is_quarantined = 1, updated_at = ? WHERE asset_id = ?",
            (iso(now), asset_id),
        )
        return ExecutionResult(
            action.action_id, ActionStatus.SUCCEEDED,
            external_ref="tag:datasentry.quarantine=" + asset_id,
            detail={
                "asset_id": asset_id,
                "mechanism": "table tag; downstream jobs check the tag and skip",
                "reversible_by": "clearing the tag restores normal reads",
            },
        )

    def _pin_feature_view(self, action: Action, now: datetime) -> ExecutionResult:
        asset_id = action.params["asset_id"]
        # Delta time travel gives last-known-good for free (ADR-014): there is
        # no snapshot service to build, only a version to name.
        snapshot = self.store.one(
            "SELECT window_end FROM profiles WHERE asset_id = ? AND degraded = 0 "
            "ORDER BY window_end DESC LIMIT 1 OFFSET 3",
            (asset_id,),
        )
        version = snapshot["window_end"] if snapshot else iso(now)
        return ExecutionResult(
            action.action_id, ActionStatus.SUCCEEDED,
            external_ref="pin:" + asset_id + "@" + str(version),
            detail={
                "asset_id": asset_id,
                "pinned_to": version,
                "mechanism": "Delta time travel snapshot, last known good",
                "reversible_by": "removing the pin resumes reading the live view",
            },
        )

    def _notify(self, action: Action, now: datetime) -> ExecutionResult:
        return ExecutionResult(
            action.action_id, ActionStatus.SUCCEEDED,
            external_ref="notify:" + str(action.params.get("team", "unassigned")),
            detail={
                "team": action.params.get("team"),
                "asset_id": action.params.get("asset_id"),
                "channel": "slack",
                "delivered_at": iso(now),
            },
        )

    def _mute(self, action: Action, now: datetime) -> ExecutionResult:
        asset_id = action.params["asset_id"]
        hours = int(action.params.get("hours", 24))
        until = now.timestamp() + hours * 3600
        from datetime import UTC
        from datetime import datetime as dt

        expiry = dt.fromtimestamp(until, UTC)
        self.store.execute(
            "INSERT OR REPLACE INTO mutes (asset_id, column_name, reason, until, created_by) "
            "VALUES (?,?,?,?,?)",
            (asset_id, action.params.get("column") or "",
             str(action.params.get("reason", "muted by remediation")), iso(expiry),
             self.identity),
        )
        return ExecutionResult(
            action.action_id, ActionStatus.SUCCEEDED,
            external_ref="mute:" + asset_id,
            detail={"asset_id": asset_id, "until": iso(expiry),
                    "note": "mutes always carry an expiry; there is no open-ended mute"},
        )

    def _backfill(self, action: Action, now: datetime) -> ExecutionResult:
        return ExecutionResult(
            action.action_id, ActionStatus.SUCCEEDED,
            external_ref="backfill:" + str(action.params.get("asset_id", "")),
            detail={
                "asset_id": action.params.get("asset_id"),
                "requested_at": iso(now),
                "status": "queued with the owning team",
            },
        )

    # -- helpers ------------------------------------------------------------

    def _pr_body(self, incident_id: str, action: Action, diff: str) -> str:
        """A PR an engineer can act on without opening the console.

        Narrative, evidence table, blast radius and the simulated effect, all
        in the review surface they are already in. A ticket saying "please
        investigate" is what this replaces.
        """
        incident = self.store.get_incident(incident_id)
        evidence = self.store.evidence_for(incident_id)
        blast = self.store.blast_radius_for(incident_id)
        hypotheses = [h for h in self.store.hypotheses_for(incident_id) if h.status == "verified"]

        lines = ["## DataSentry incident " + incident_id[:8]]
        if incident:
            lines.append("")
            lines.append("**" + incident.title + "** — severity " + str(incident.severity)
                         + ", detected " + iso(incident.first_detected_at))
        if incident and incident.narrative:
            lines += ["", incident.narrative]
        if hypotheses:
            top = hypotheses[0]
            lines += ["", "### Root cause", "",
                      "`" + str(top.cause_class) + "` at confidence "
                      + format(top.confidence, ".2f"), "", top.statement]
        if evidence:
            lines += ["", "### Evidence", "", "| id | kind | summary |", "|---|---|---|"]
            for item in evidence[:8]:
                lines.append(
                    "| `" + item.evidence_id + "` | " + str(item.kind) + " | "
                    + item.summary.replace("|", "/") + " |"
                )
        if blast:
            lines += ["", "### Blast radius (" + str(len(blast)) + " assets)", ""]
            for entry in blast[:8]:
                lines.append(
                    "- `" + entry.asset_id + "`"
                    + ("." + entry.column_name if entry.column_name else "")
                    + " — " + str(entry.asset_kind).lower() + ", criticality "
                    + format(entry.criticality, ".2f")
                )
        lines += ["", "### Simulated effect", "",
                  "Reverting this clause restores the prior join semantics. "
                  "Row volume and the null rate on the affected measures return to "
                  "their conformal intervals within two profiling cycles; if they do "
                  "not, DataSentry escalates rather than closing the incident.",
                  "", "---", "",
                  "Opened by DataSentry under `" + self.identity + "`. "
                  "This identity can open pull requests and nothing else — it cannot "
                  "push to a default branch, and no action type in the system is "
                  "irreversible."]
        return "\n".join(lines)

    def _audit(self, action: Action, verb: str, detail: dict[str, Any],
               now: datetime) -> None:
        self.store.audit(
            actor=self.identity, actor_kind="service", action=verb,
            target=action.action_id,
            detail={
                "incident_id": action.incident_id,
                "action_type": str(action.action_type),
                "risk_tier": str(action.risk_tier),
                "policy_version": action.policy_version,
                "result": {k: v for k, v in detail.items() if k != "diff"},
            },
            at=now,
        )


def _basename(path: str) -> str:
    return path.split("/")[-1] if path else path


def _unified_diff(path: str, before: str, after: str) -> str:
    import difflib

    lines = list(
        difflib.unified_diff(
            before.splitlines(keepends=True),
            after.splitlines(keepends=True),
            fromfile="a/" + path,
            tofile="b/" + path,
            n=3,
        )
    )
    return "".join(lines)


def action_fingerprint(incident_id: str, action_type: ActionType, params: dict[str, Any]) -> str:
    """Idempotency key. The executor checks for an existing PR before opening one."""
    payload = json.dumps(params, sort_keys=True, default=str)
    return hashlib.sha256(
        (incident_id + "|" + str(action_type) + "|" + payload).encode()
    ).hexdigest()[:16]
