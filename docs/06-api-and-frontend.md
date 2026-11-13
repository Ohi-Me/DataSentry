# 06 — API & Frontend

## 1. REST API (FastAPI, `/api/v1`)

All responses are Pydantic models. All list endpoints are cursor-paginated. All mutating endpoints require an `Idempotency-Key` header.

### Incidents

```
GET    /incidents
         ?status=&severity=&asset_id=&since=&cursor=&limit=50
         → { items: [IncidentSummary], next_cursor }

GET    /incidents/{incident_id}
         → IncidentDetail {
             incident, anomalies[], hypotheses[], evidence[],
             blast_radius[], actions[], timeline[], cost
           }

GET    /incidents/{incident_id}/trace
         → AgentTrace { nodes[], transitions[], tool_calls[], tokens, cost }

POST   /incidents/{incident_id}/disposition
         { verdict, correct_cause_class?, notes? } → 201

POST   /incidents/{incident_id}/rerun-rca
         { prompt_version? } → 202 { run_id }     # replay against frozen evidence

POST   /incidents/{incident_id}/mute
         { until, reason } → 201
```

### Actions

```
GET    /actions?status=proposed&risk_tier=
POST   /actions/{action_id}/approve   { note? } → 202
POST   /actions/{action_id}/reject    { reason }  → 200
GET    /actions/{action_id}           → ActionDetail (incl. PR status)
```

### Assets & lineage

```
GET    /assets?tier=&layer=&search=
GET    /assets/{asset_id}
GET    /assets/{asset_id}/profile
         ?column=&signal=&cycles=90
         → ProfileSeries { points[], baseline_band[], anomaly_markers[] }
GET    /assets/{asset_id}/lineage
         ?column=&direction=both&depth=3
         → LineageGraph { nodes[], edges[] }
GET    /assets/{asset_id}/blast-radius?column=
```

### Platform health

```
GET    /health/detectors    → per-signal observed vs target FPR, precision, recall
GET    /health/pipeline     → consumer lag, profiling coverage %, lineage parse coverage %
GET    /health/agent        → gold-set scores for the deployed prompt version, cost trend
GET    /metrics             → Prometheus exposition
```

`/health/detectors` and `/health/agent` are product surfaces, not internal endpoints. A system that publishes its own accuracy is the thesis of the whole project.

### WebSocket

```
WS /ws/incidents
  → { type: "incident.created" | "incident.updated" | "action.requires_approval"
              | "rca.node_completed", payload: {...} }
```

`rca.node_completed` streams agent progress live. Watching hypotheses appear and get rejected in real time is the demo moment.

## 2. Authentication & authorization

- OIDC (Auth0/Cognito) → JWT; `api` validates and maps to a user record.
- Roles: `viewer` (read), `operator` (dispose, approve low/medium), `admin` (approve high, edit policy, mute indefinitely).
- Every mutating call writes an `audit_log` row via middleware — not per-endpoint code, so it can't be forgotten.
- Service-to-service uses mTLS within the VPC plus a signed service identity header.

## 3. Frontend

**Stack:** Next.js 14 App Router, TypeScript strict, Tailwind, shadcn/ui, TanStack Query, React Flow (lineage), Recharts (time series), Zustand (ephemeral UI state only).

### 3.1 Screens

| Route | Purpose |
|---|---|
| `/` | Incident inbox — sorted by severity × criticality, not by time |
| `/incidents/[id]` | Incident workspace (the core screen) |
| `/incidents/[id]/replay` | Agent reasoning replay |
| `/assets` | Asset browser with health status |
| `/assets/[id]` | Profile history, lineage, incident history |
| `/approvals` | Action approval tray |
| `/health` | Detector calibration, pipeline health, agent eval scores |
| `/policies` | Declarative policy editor with version history |

### 3.2 Incident workspace layout

```
┌────────────────────────────────────────────────────────────────┐
│ INC-0412  severity 1  fct_orders.revenue_usd     [Dispose ▾]   │
│ status: awaiting_approval · detected 14:03 · RCA 74s · $0.11   │
├──────────────────────────┬─────────────────────────────────────┤
│ TIMELINE                 │ ROOT CAUSE                          │
│  ┌──── anomaly onset     │ JOIN_SEMANTICS   confidence 0.87    │
│  │  ◆ commit a3f9c1      │ "stg_orders changed INNER→LEFT on   │
│  │  ▲ dbt run #8821      │  customer_id in PR #4471, admitting │
│  │  ● anomaly detected   │  unmatched rows with NULL revenue"  │
│  └──── now               │                                     │
│  [volume ▂▃▂▃█▇ chart]   │ EVIDENCE (4)                        │
│  [null_rate chart w/     │  ev_7f3a  SQL semantic diff    ↗    │
│   conformal band]        │  ev_9c21  commit a3f9c1        ↗    │
│                          │  ev_1b04  profile delta        ↗    │
├──────────────────────────┤  ev_44de  dbt run #8821        ↗    │
│ BLAST RADIUS (11)        │                                     │
│  ⚠ ml_features.rev_30d   │ REJECTED HYPOTHESES (2)      [show] │
│  ⚠ dash_emea_revenue     ├─────────────────────────────────────┤
│    fct_orders_daily      │ PROPOSED ACTION      risk: medium   │
│    ... 8 more            │  OPEN_PR  revert join in stg_orders │
│                          │  [ view diff ]  [ Approve ] [ Rej ] │
└──────────────────────────┴─────────────────────────────────────┘
```

Design decisions that matter:
- **Timeline and profile charts share one x-axis.** Seeing the commit marker land exactly at the inflection point is the "oh" moment. Don't put them in separate cards.
- **Rejected hypotheses are visible**, collapsed by default. Showing the reasoning the system discarded is what makes people trust the reasoning it kept.
- **Every evidence row is clickable** and opens the raw artifact (the actual diff, the actual query). One click from claim to proof.
- **Cost and latency are in the header.** Not hidden in a metrics page.

### 3.3 Agent replay view

Vertical stepper of node transitions. Each node card shows: inputs (collapsed), the model used, tokens, duration, and the parsed output rendered as a typed object — not raw JSON. Hypotheses show their claims with evidence chips; hovering a chip highlights the evidence in a side panel. A scrubber replays the run at 4× speed.

Build this early. It is simultaneously your best debugging tool and your best demo artifact.

### 3.4 Lineage canvas

React Flow with a custom column-level node renderer:
- Table nodes expand to column nodes on click.
- Impacted paths are highlighted; unaffected branches dim to 20% opacity.
- Edge styling encodes `transform_type` (solid = identity, dashed = expression, dotted = observed-only/inferred).
- Depth slider (1–8) with server-side pruning — never ship the full graph to the browser.
- Layout: dagre, left-to-right, cached per (asset, depth) since layout is expensive.

Performance target: 60fps interaction on a 500-node subgraph. Above that, collapse to table level automatically and show a "expand columns" affordance.

### 3.5 Approval tray

A queue optimized for throughput, keyboard-first: `j`/`k` to move, `a` to approve, `r` to reject, `?` for help. Each item shows action type, risk tier, blast radius size, the diff, and the one-line rationale. Bulk approve is deliberately **not** offered for `high` risk.

### 3.6 Health page

Four panels:
1. **Detector calibration** — observed vs. target FPR per signal, per tier, as a scatter with the y=x line. Points off the line are the story.
2. **Agent quality** — gold-set scores over prompt versions, as a small multiple.
3. **Pipeline** — consumer lag, profiling coverage %, lineage parse coverage %.
4. **Economics** — cost per incident over time, cheap/frontier model split, cache hit rate.

## 4. Real-time strategy

- Incident list and detail subscribe via WebSocket; TanStack Query cache is patched on message, not invalidated (avoids refetch storms during an incident burst).
- Reconnect with exponential backoff; on reconnect, fetch a delta since the last received event ID rather than a full refresh.
- Server fans out from a Kafka consumer in the `api` service to per-user channels filtered by RBAC scope.

## 5. Accessibility & polish (cheap, high signal)

- Keyboard navigation throughout; visible focus rings.
- Severity is never encoded by colour alone (icon + label).
- Empty states that teach ("No incidents. 1,847 columns profiled in the last hour; last incident 3 days ago.").
- Skeleton loaders sized to the real content so the layout doesn't jump.

These take a day total and are the difference between "student project" and "product" at first glance.
