"""A reasoner with no language model in it.

This exists for three reasons, in order of how much each one matters:

1. **It is the baseline.** An LLM-based RCA that cannot beat a rule table on
   the gold set has not earned its cost. Running both against the same 40
   incidents turns "the agent works" into a number with a comparison in it.
2. **It makes the eval suite deterministic.** Same seed, same incidents, same
   output, every time — which is what lets CI gate on a regression rather
   than on noise.
3. **It is the degradation path.** When the model provider is down, incidents
   still get a cited root cause and a proposed action, just written less
   fluently. The failure-mode table calls for the incident to survive an LLM
   outage; this is how.

It reasons the way an experienced engineer does on the first pass: match the
observed signal signature against known fault mechanisms, check whether a
code change on the lineage path could have produced it, and prefer the single
cause that explains everything over several that each explain one thing.
"""

from __future__ import annotations

from typing import Any

from datasentry_agent.providers.base import (
    ActionDraft,
    ActionPlanResult,
    ClaimDraft,
    HypothesisBatch,
    HypothesisDraft,
    NarrativeResult,
    TriageDecision,
    Usage,
    VerificationBatch,
    VerificationDraft,
    prompt_version,
)
from datasentry_core.enums import ActionType, CauseClass

# Signal signatures, most specific first. Each entry is
# (cause class, required signals, supporting signals, base confidence).
SIGNATURES: list[tuple[CauseClass, set[str], set[str], float]] = [
    (CauseClass.SOURCE_SCHEMA_DRIFT, {"SCHEMA"}, set(), 0.88),
    (CauseClass.DEDUP_BREAK, {"VOLUME", "CARDINALITY"}, set(), 0.82),
    (CauseClass.JOIN_SEMANTICS, {"VOLUME", "NULL_RATE"}, {"CARDINALITY"}, 0.80),
    (CauseClass.UNIT_CHANGE, {"NUMERIC_DRIFT"}, set(), 0.74),
    (CauseClass.LATE_DATA, {"FRESHNESS"}, {"VOLUME"}, 0.78),
    (CauseClass.SOURCE_SCHEMA_DRIFT, {"CATEGORICAL_DRIFT"}, {"NEW_CATEGORY"}, 0.70),
    (CauseClass.SOURCE_SCHEMA_DRIFT, {"STRING_LENGTH"}, {"CARDINALITY"}, 0.72),
    (CauseClass.SOURCE_SCHEMA_DRIFT, {"NEW_CATEGORY"}, set(), 0.68),
    # Single-signal fallbacks. Less specific than the combinations above and
    # ranked accordingly, but a NULL_RATE break with no corroborating signal
    # still has a most-likely mechanism, and "no hypothesis" is a worse answer
    # than a correctly hedged one.
    (CauseClass.SOURCE_SCHEMA_DRIFT, {"NULL_RATE"}, {"CARDINALITY"}, 0.62),
    (CauseClass.DEDUP_BREAK, {"CARDINALITY"}, set(), 0.58),
    (CauseClass.LATE_DATA, {"VOLUME"}, {"FRESHNESS"}, 0.52),
    (CauseClass.VOLUME_SHIFT, {"VOLUME"}, set(), 0.50),
    (CauseClass.INFRA_FAILURE, {"MULTIVARIATE"}, set(), 0.45),
]

MECHANISM = {
    CauseClass.JOIN_SEMANTICS: (
        "a join in the model producing {asset} was loosened, admitting unmatched "
        "rows with NULL measures — which is why volume rose while the null rate "
        "on {column} rose with it"
    ),
    CauseClass.DEDUP_BREAK: (
        "the deduplication key on {asset} stopped covering every component, so "
        "rows fan out: the row count rose while distinct values held, collapsing "
        "the distinctness ratio on {column}"
    ),
    CauseClass.UNIT_CHANGE: (
        "values in {asset}.{column} shifted by a constant multiplicative factor "
        "with no schema change, which is the signature of a unit or currency "
        "switch upstream rather than a change in the underlying business"
    ),
    CauseClass.LATE_DATA: (
        "the upstream load for {asset} is late: freshness is outside its interval "
        "and the partition is short, so the volume shortfall is arrival timing "
        "rather than lost records"
    ),
    CauseClass.SOURCE_SCHEMA_DRIFT: (
        "the upstream contract for {asset}.{column} changed — a retype, a rename "
        "or a widened value domain — without a corresponding change in this model"
    ),
    CauseClass.UPSTREAM_CODE_CHANGE: (
        "a change to the model producing {asset} altered its semantics; the "
        "structured diff shows the specific clause that moved"
    ),
    CauseClass.VOLUME_SHIFT: (
        "the row volume of {asset} moved outside its seasonal interval with no "
        "accompanying change in value distribution or nullability"
    ),
    CauseClass.INFRA_FAILURE: (
        "the joint profile of {asset} is in a region it has not previously "
        "occupied, though no individual signal is out of range on its own"
    ),
    CauseClass.SEASONAL_LEGITIMATE: (
        "the movement on {asset} is consistent with seasonal variation or an "
        "announced event rather than a defect"
    ),
}


# Which read-only tools would confirm or refute each cause class. The
# hypothesis names what it needs; GatherEvidence goes and gets it.
EVIDENCE_REQUESTS = {
    CauseClass.UPSTREAM_CODE_CHANGE: ["get_sql_semantic_diff", "diff_git_range", "get_dbt_runs"],
    CauseClass.JOIN_SEMANTICS: ["get_sql_semantic_diff", "get_lineage_neighborhood"],
    CauseClass.DEDUP_BREAK: ["get_sql_semantic_diff", "get_profile_history"],
    CauseClass.UNIT_CHANGE: ["get_profile_history", "get_schema_changes", "get_query_history"],
    CauseClass.SOURCE_SCHEMA_DRIFT: ["get_schema_changes", "get_upstream_anomalies"],
    CauseClass.LATE_DATA: ["get_dbt_runs", "get_query_history"],
    CauseClass.VOLUME_SHIFT: ["get_profile_history", "get_upstream_anomalies"],
    CauseClass.INFRA_FAILURE: ["get_dbt_runs", "get_query_history"],
    CauseClass.SEASONAL_LEGITIMATE: ["get_profile_history", "search_past_incidents"],
}


def _requests_for(cause: CauseClass) -> list[str]:
    return list(EVIDENCE_REQUESTS.get(cause, ["get_profile_history"]))


class HeuristicReasoner:
    name = "heuristic"
    model = "heuristic"

    def __init__(self) -> None:
        self.prompt_version = prompt_version()

    # -- triage -------------------------------------------------------------

    def triage(self, context: dict[str, Any]) -> TriageDecision:
        incident = context.get("incident", {})
        muted = context.get("muted", False)
        duplicate = context.get("duplicate_of")

        if muted:
            return TriageDecision(False, reason="an active mute covers this asset and signal")
        if duplicate:
            return TriageDecision(False, reason="duplicate of open incident " + str(duplicate))

        severity = int(incident.get("severity", 3))
        tier = int(context.get("root_tier", 3))
        blast = len(context.get("downstream", []))
        if severity == 1 or tier == 1 or blast > 20:
            depth = "deep"
        elif tier == 3 and blast == 0:
            depth = "shallow"
        else:
            depth = "standard"
        return TriageDecision(
            True,
            depth=depth,
            reason=(
                "tier-" + str(tier) + " root asset with " + str(blast)
                + " downstream assets at severity " + str(severity)
            ),
        )

    # -- hypothesise --------------------------------------------------------

    def hypothesize(self, context: dict[str, Any]) -> HypothesisBatch:
        signals = set(context.get("signals", []))
        asset = _short(context.get("incident", {}).get("root_asset_id", ""))
        column = context.get("incident", {}).get("root_column") or "the affected column"
        evidence = context.get("evidence", [])
        commits = context.get("commits", [])
        rejected = {r.get("cause_class") for r in context.get("rejected", [])}

        by_kind = _index_evidence(evidence)
        drafts: list[HypothesisDraft] = []

        breaking_commit = _breaking_commit(commits)
        if breaking_commit is not None and str(CauseClass.UPSTREAM_CODE_CHANGE) not in rejected:
            drafts.append(self._code_change_draft(breaking_commit, asset, column, by_kind, signals))

        for cause, required, supporting, base in SIGNATURES:
            if str(cause) in rejected:
                continue
            if not required <= signals:
                continue
            if any(d.cause_class == cause for d in drafts):
                continue
            explained = len((required | supporting) & signals)
            confidence = min(0.95, base + 0.04 * (explained - len(required)))
            drafts.append(
                HypothesisDraft(
                    cause_class=cause,
                    statement=MECHANISM[cause].format(asset=asset, column=column),
                    claims=self._claims_for(cause, asset, column, by_kind, context),
                    confidence=round(confidence, 3),
                    evidence_requests=_requests_for(cause),
                )
            )
            if len(drafts) >= 4:
                break

        # Always carry a benign explanation -- an investigation that cannot
        # conclude "nothing is wrong" will never conclude it -- except where a
        # deterministic signal fired. A schema hash changing is a fact about
        # the world, and no amount of seasonality explains a column changing
        # type. Offering the benign option there is not open-mindedness, it is
        # an invitation to dismiss a real incident.
        deterministic_fired = bool(signals & {"SCHEMA"})
        if (
            not deterministic_fired
            and str(CauseClass.SEASONAL_LEGITIMATE) not in rejected
            and len(drafts) < 5
        ):
            drafts.append(
                HypothesisDraft(
                    cause_class=CauseClass.SEASONAL_LEGITIMATE,
                    statement=MECHANISM[CauseClass.SEASONAL_LEGITIMATE].format(
                        asset=asset, column=column
                    ),
                    claims=[
                        ClaimDraft(
                            text=(
                                "the observed movement is within the range this series has "
                                "reached before under seasonal load"
                            ),
                            evidence_ids=_ids(by_kind.get("PROFILE_DELTA", [])[:1]),
                        )
                    ],
                    confidence=0.22,
                    evidence_requests=["get_profile_history"],
                )
            )

        drafts.sort(key=lambda d: -d.confidence)
        for draft in drafts:
            draft.claims = [c for c in draft.claims if c.evidence_ids]
        drafts = [d for d in drafts if d.claims][:5]

        return HypothesisBatch(
            hypotheses=drafts,
            usage=Usage(model="heuristic", tier="frontier", tokens_in=0, tokens_out=0),
        )

    def _code_change_draft(self, commit: dict[str, Any], asset: str, column: str,
                           by_kind: dict[str, list], signals: set[str]) -> HypothesisDraft:
        diff = commit.get("semantic_diff") or {}
        changes = diff.get("changes", [])
        breaking = [c for c in changes if c.get("severity") == "breaking"]
        headline = breaking[0]["detail"] if breaking else diff.get("headline", "the model changed")

        cause = CauseClass.UPSTREAM_CODE_CHANGE
        if any(c.get("kind") in {"join_type", "join_key"} for c in breaking):
            cause = CauseClass.JOIN_SEMANTICS
        elif any(c.get("kind") == "filter" for c in breaking):
            cause = CauseClass.VOLUME_SHIFT

        sql_ids = _ids([e for e in by_kind.get("SQL_DIFF", []) if e["ref"] == commit["sha"]]) \
            or _ids(by_kind.get("SQL_DIFF", [])[:1])
        commit_ids = _ids([e for e in by_kind.get("GIT_COMMIT", []) if e["ref"] == commit["sha"]])
        profile_ids = _ids(by_kind.get("PROFILE_DELTA", [])[:1])
        lineage_ids = _ids(by_kind.get("LINEAGE_PATH", [])[:1])

        # Each claim cites everything that bears on it. The structured diff is
        # itself commit evidence — its ref *is* the commit sha — so a claim
        # about what the change did is supported by the diff whether or not
        # the commit log tool also returned something.
        change_ids = sql_ids + commit_ids
        claims = [
            ClaimDraft(text=headline, evidence_ids=change_ids),
            ClaimDraft(
                text=(
                    "the change landed in " + commit.get("model_ref", "the model")
                    + " which sits on the lineage path to " + asset + "." + str(column)
                ),
                evidence_ids=lineage_ids + change_ids,
            ),
            ClaimDraft(
                text=(
                    "the anomaly onset follows the commit rather than preceding it, and the "
                    "direction of the profile shift matches what the change would produce"
                ),
                evidence_ids=profile_ids + change_ids,
            ),
        ]
        return HypothesisDraft(
            cause_class=cause,
            statement=(
                _short(commit.get("model_ref", "a model")) + " changed in "
                + ("PR #" + str(commit["pr_number"]) if commit.get("pr_number") else "commit " + commit["sha"][:7])
                + ": " + headline
            ),
            claims=[c for c in claims if c.evidence_ids],
            confidence=0.90 if breaking else 0.6,
            evidence_requests=["get_sql_semantic_diff", "diff_git_range", "get_profile_history"],
        )

    def _claims_for(self, cause: CauseClass, asset: str, column: str,
                    by_kind: dict[str, list], context: dict[str, Any]) -> list[ClaimDraft]:
        profile = _ids(by_kind.get("PROFILE_DELTA", [])[:2])
        upstream = _ids(by_kind.get("UPSTREAM_ANOMALY", [])[:1])
        schema = _ids(by_kind.get("SCHEMA_CHANGE", [])[:1])
        lineage = _ids(by_kind.get("LINEAGE_PATH", [])[:1])
        runs = _ids(by_kind.get("DBT_RUN", [])[:1])

        detail = _anomaly_detail(context)
        claims = [ClaimDraft(text=detail, evidence_ids=profile)]

        if cause == CauseClass.SOURCE_SCHEMA_DRIFT and schema:
            claims = [
                ClaimDraft(
                    text=(
                        "a schema change on this asset was recorded in the same window, "
                        "which is a fact rather than an inference"
                    ),
                    evidence_ids=[*schema, *profile],
                ),
                *claims,
            ]
        if cause in {CauseClass.JOIN_SEMANTICS, CauseClass.DEDUP_BREAK} and lineage:
            claims.append(
                ClaimDraft(
                    text=(
                        "the affected columns share a single upstream model, so one change "
                        "there explains all of them rather than several independent ones"
                    ),
                    evidence_ids=lineage,
                )
            )
        if cause == CauseClass.LATE_DATA and runs:
            claims.append(
                ClaimDraft(
                    text="the most recent load job for this asset ran late or short",
                    evidence_ids=runs,
                )
            )
        if upstream and cause != CauseClass.SEASONAL_LEGITIMATE:
            claims.append(
                ClaimDraft(
                    text="an ancestor asset broke first in the same window, so the root is upstream",
                    evidence_ids=upstream,
                )
            )
        return [c for c in claims if c.evidence_ids]

    # -- verify -------------------------------------------------------------

    def verify(self, context: dict[str, Any]) -> VerificationBatch:
        """Adversarial by construction: reject unless the evidence compels."""
        evidence = {e["id"]: e for e in context.get("evidence", [])}
        hypotheses = context.get("hypotheses", [])
        signals = set(context.get("signals", []))
        commits = context.get("commits", [])
        results: list[VerificationDraft] = []

        for index, hypothesis in enumerate(hypotheses):
            cause = hypothesis.get("cause_class")
            claims = hypothesis.get("claims", [])
            unsupported: list[int] = []
            reasons: list[str] = []

            for ci, claim in enumerate(claims):
                ids = claim.get("evidence_ids", [])
                if not ids:
                    unsupported.append(ci)
                    reasons.append("claim " + str(ci + 1) + " cites no evidence")
                    continue
                missing = [i for i in ids if i not in evidence]
                if missing:
                    # The hallucination tripwire. A fabricated id is not a
                    # weak citation, it is a fabricated citation.
                    unsupported.append(ci)
                    reasons.append(
                        "claim " + str(ci + 1) + " cites evidence that does not exist ("
                        + ", ".join(missing) + ")"
                    )
                    continue
                if not _entails(claim, [evidence[i] for i in ids], cause):
                    unsupported.append(ci)
                    reasons.append(
                        "claim " + str(ci + 1) + " cites evidence that is consistent with it "
                        "but does not entail it"
                    )

            # Signature check. A cause class implies a signal pattern, and a
            # hypothesis whose pattern is entirely absent is asserting a
            # mechanism nothing observed.
            #
            # Two deliberate limits on how hard this bites:
            #
            # * It rejects only when *none* of the expected signals fired.
            #   Requiring all of them would reject a correct diagnosis for
            #   missing its weakest corroborating signal, and the weakest
            #   signal is missing all the time -- volume moves less than
            #   nullability under the same fault, and a cold table may not
            #   have been profiled twice yet.
            # * It is skipped entirely when the hypothesis cites direct
            #   mechanism evidence. A structured diff showing INNER became
            #   LEFT is a statement about what the code now does. "We did not
            #   also see a volume anomaly" does not outrank it.
            cited_kinds = {
                evidence[i]["kind"]
                for claim in claims for i in claim.get("evidence_ids", []) if i in evidence
            }
            has_direct_mechanism = bool(cited_kinds & {"SQL_DIFF", "SCHEMA_CHANGE"})
            required = _required_signals(cause)
            if required and not has_direct_mechanism and not (required & signals):
                reasons.append(
                    "this cause implies " + ", ".join(sorted(required))
                    + ", and none of those signals fired"
                )
                results.append(
                    VerificationDraft(
                        hypothesis_index=index, status="rejected",
                        rejection_reason="; ".join(reasons), unsupported_claims=unsupported,
                        adjusted_confidence=0.1,
                    )
                )
                continue

            if (cause in {str(CauseClass.UPSTREAM_CODE_CHANGE),
                          str(CauseClass.JOIN_SEMANTICS)}
                    and not _breaking_commit(commits)):
                    reasons.append(
                        "no commit in the detection window touches the lineage path of the "
                        "affected columns, so timing correlation alone is doing the work"
                    )
                    results.append(
                        VerificationDraft(
                            hypothesis_index=index, status="rejected",
                            rejection_reason="; ".join(reasons),
                            unsupported_claims=unsupported, adjusted_confidence=0.15,
                        )
                    )
                    continue

            if unsupported:
                results.append(
                    VerificationDraft(
                        hypothesis_index=index, status="rejected",
                        rejection_reason="; ".join(reasons), unsupported_claims=unsupported,
                        adjusted_confidence=round(float(hypothesis.get("confidence", 0.5)) * 0.4, 3),
                    )
                )
                continue

            base = float(hypothesis.get("confidence", 0.5))
            corroboration = min(0.08, 0.02 * sum(len(c.get("evidence_ids", [])) for c in claims))
            results.append(
                VerificationDraft(
                    hypothesis_index=index, status="verified",
                    adjusted_confidence=round(min(0.97, base + corroboration), 3),
                )
            )

        return VerificationBatch(
            results=results,
            usage=Usage(model="heuristic", tier="frontier"),
        )

    # -- narrate ------------------------------------------------------------

    def narrate(self, context: dict[str, Any]) -> NarrativeResult:
        top = context.get("top_hypothesis") or {}
        incident = context.get("incident", {})
        downstream = context.get("downstream", [])
        actions = context.get("actions", [])
        asset = _short(incident.get("root_asset_id", ""))
        column = incident.get("root_column")
        target = asset + ("." + column if column else "")
        detail = _anomaly_detail(context)
        cited = ", ".join(top.get("evidence_ids", [])[:4]) or "the collected evidence"

        first = (
            target + " broke at " + _time(incident.get("first_detected_at"))
            + ". " + detail[0].upper() + detail[1:] + "."
        )
        second = top.get("statement", "The cause could not be established.")
        if not second.endswith("."):
            second += "."
        second = second[0].upper() + second[1:] + " (" + cited + ")"

        if downstream:
            ranked = downstream[:3]
            names = ", ".join(_short(d.get("asset_id", "")) for d in ranked)
            more = len(downstream) - len(ranked)
            third = (
                str(len(downstream)) + " downstream assets consumed the affected columns, "
                "led by " + names + (" and " + str(more) + " more" if more > 0 else "") + ". "
            )
        else:
            third = "No downstream consumer read the affected columns in this window. "
        if actions:
            third += (
                "The proposed remediation is "
                + ", ".join(str(a.get("action_type", "")) for a in actions)
                + ", each reversible and each requiring the policy gate to clear it."
            )
        else:
            third += "No automated remediation applies to this cause class."

        return NarrativeResult(
            text=first + "\n\n" + second + "\n\n" + third,
            usage=Usage(model="heuristic", tier="frontier"),
        )

    # -- plan ---------------------------------------------------------------

    def plan(self, context: dict[str, Any]) -> ActionPlanResult:
        top = context.get("top_hypothesis") or {}
        cause = top.get("cause_class")
        incident = context.get("incident", {})
        downstream = context.get("downstream", [])
        asset_id = incident.get("root_asset_id", "")
        owner = context.get("owner_team", "unassigned")
        commit = _breaking_commit(context.get("commits", []))
        actions: list[ActionDraft] = []

        if cause in {str(CauseClass.UPSTREAM_CODE_CHANGE), str(CauseClass.JOIN_SEMANTICS),
                     str(CauseClass.VOLUME_SHIFT)} and commit is not None:
            actions.append(
                ActionDraft(
                    action_type=ActionType.OPEN_PR,
                    params={
                        "repo": commit.get("repo", "analytics"),
                        "model_ref": commit.get("model_ref", ""),
                        "revert_sha": commit.get("sha", ""),
                        "branch": "datasentry/revert-" + str(commit.get("sha", ""))[:7],
                        "incident_id": incident.get("incident_id", ""),
                    },
                    rationale=(
                        "revert the specific clause the semantic diff identifies, leaving the "
                        "rest of the commit intact"
                    ),
                )
            )

        if cause in {str(CauseClass.SOURCE_SCHEMA_DRIFT), str(CauseClass.UNIT_CHANGE),
                     str(CauseClass.DEDUP_BREAK), str(CauseClass.LATE_DATA)}:
            actions.append(
                ActionDraft(
                    action_type=ActionType.NOTIFY_OWNER,
                    params={"team": owner, "asset_id": asset_id,
                            "incident_id": incident.get("incident_id", "")},
                    rationale="the cause is upstream of this repository and needs the owning team",
                )
            )

        feature_views = [d for d in downstream if d.get("asset_kind") == "FEATURE_VIEW"]
        for view in feature_views[:2]:
            actions.append(
                ActionDraft(
                    action_type=ActionType.PIN_FEATURE_VIEW,
                    params={"asset_id": view["asset_id"], "pin_to": "last_known_good",
                            "incident_id": incident.get("incident_id", "")},
                    rationale=(
                        "this feature view reads an affected column; pinning to the last "
                        "known good snapshot stops models training on corrupted data"
                    ),
                )
            )

        tier_one_downstream = [d for d in downstream if int(d.get("tier", 3)) == 1]
        if tier_one_downstream and cause not in {str(CauseClass.SEASONAL_LEGITIMATE), None}:
            actions.append(
                ActionDraft(
                    action_type=ActionType.QUARANTINE_TABLE,
                    params={"asset_id": asset_id, "incident_id": incident.get("incident_id", "")},
                    rationale=(
                        str(len(tier_one_downstream)) + " tier-1 assets read this table and the "
                        "data is actively wrong, not merely suspicious"
                    ),
                )
            )

        if cause == str(CauseClass.SEASONAL_LEGITIMATE):
            actions = [
                ActionDraft(
                    action_type=ActionType.MUTE_ALERT,
                    params={"asset_id": asset_id, "hours": 24,
                            "reason": "movement consistent with seasonal load"},
                    rationale="a bounded mute with an expiry, never an open-ended one",
                )
            ]

        if not actions:
            actions.append(
                ActionDraft(
                    action_type=ActionType.NOTIFY_OWNER,
                    params={"team": owner, "asset_id": asset_id,
                            "incident_id": incident.get("incident_id", "")},
                    rationale="no automated remediation matches this cause class",
                )
            )

        return ActionPlanResult(actions=actions, usage=Usage(model="heuristic", tier="cheap"))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _short(asset_id: str) -> str:
    parts = asset_id.split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else asset_id


def _time(value: Any) -> str:
    text = str(value or "")
    return text[11:16] if len(text) >= 16 else text


def _ids(items: list) -> list[str]:
    return [i["id"] for i in items]


def _index_evidence(evidence: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for item in evidence:
        out.setdefault(item.get("kind", ""), []).append(item)
    return out


def _breaking_commit(commits: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The most recent commit that both touches the lineage path and changes semantics.

    Both conditions. A commit that landed in the window but does not touch the
    path is exactly the post-hoc-ergo-propter-hoc trap the verifier exists to
    catch, so it does not qualify here either.
    """
    ranked = [
        c for c in commits
        if c.get("touches_lineage")
        and (c.get("semantic_diff") or {}).get("breaking_count", 0) > 0
    ]
    ranked.sort(key=lambda c: str(c.get("at", "")), reverse=True)
    return ranked[0] if ranked else None


def _required_signals(cause: str | None) -> set[str]:
    for candidate, required, _supporting, _base in SIGNATURES:
        if str(candidate) == cause:
            return set(required)
    return set()


def _entails(claim: dict[str, Any], evidence: list[dict[str, Any]], cause: str | None) -> bool:
    """Does the cited evidence entail the claim, or is it merely consistent?

    The distinction is the entire value of the verifier. A profile delta is
    consistent with any cause; it entails a claim only when that claim is
    about the measurement itself. A claim about a code change needs a diff.
    """
    kinds = {e.get("kind") for e in evidence}
    text = claim.get("text", "").lower()

    if "onset" in text or "follows the commit" in text:
        # A claim about timing asserts two things -- that something changed
        # and that the measurement moved after it. Both have to be cited, or
        # the claim is half-evidenced and the missing half is the causal one.
        return bool(kinds & {"SQL_DIFF", "GIT_COMMIT", "SCHEMA_CHANGE"}) and "PROFILE_DELTA" in kinds
    if "commit" in text or "changed" in text or "join" in text or "landed" in text:
        return bool(kinds & {"SQL_DIFF", "GIT_COMMIT", "SCHEMA_CHANGE"})
    if "lineage path" in text or "upstream model" in text:
        return bool(kinds & {"LINEAGE_PATH", "GIT_COMMIT"})
    if "ran late" in text or "job" in text:
        return bool(kinds & {"DBT_RUN", "QUERY_LOG"})
    if "ancestor" in text or "broke first" in text:
        return "UPSTREAM_ANOMALY" in kinds
    if "seasonal" in text or "range this series" in text:
        # A benign explanation has to survive the measurement it is explaining
        # away. If the cited profile delta shows the series outside its own
        # baseline, "this is normal variation" is contradicted by the very
        # evidence offered for it -- and a verifier that accepts a claim its
        # own evidence refutes is decoration.
        for item in evidence:
            if item.get("kind") != "PROFILE_DELTA":
                continue
            if (item.get("payload") or {}).get("changed"):
                return False
        return "PROFILE_DELTA" in kinds
    return bool(kinds)


def _anomaly_detail(context: dict[str, Any]) -> str:
    anomalies = context.get("anomalies", [])
    if not anomalies:
        return "an anomaly was detected with no measurement attached"
    ranked = sorted(anomalies, key=lambda a: -float(a.get("score", 0)))
    lead = ranked[0]
    detail = lead.get("explanation") or (
        str(lead.get("signal")) + " moved from " + format(float(lead.get("baseline", 0)), ".4g")
        + " to " + format(float(lead.get("observed", 0)), ".4g")
    )
    if len(ranked) > 1:
        detail += " (with " + str(len(ranked) - 1) + " further signals breaking in the same window)"
    return detail
