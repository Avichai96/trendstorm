# Security Incident Runbook

**Alerts covered**: `SSRFBlockBurst`, `PromptInjectionDetected`, `PIIDetectionBurst`, `AuditLogVolumeSurge`

Runbook URI convention: `https://github.com/trendstorm-ai/trendstorm/ops/runbooks/security-incident.md`

---

## 1. SSRF Block Alert

**Alert**: `SSRFBlockBurst`
**Expression**: `rate(trendstorm_security_block_total{reason=~"ssrf_.*"}[5m]) > 1`
**Severity**: warning at >1/min; page at >10/min sustained for 3 minutes.

### Detection

```promql
# Current rate of SSRF blocks by reason
rate(trendstorm_security_block_total{reason=~"ssrf_.*"}[5m])

# Top reason breakdown
topk(10, sum by (reason) (rate(trendstorm_security_block_total{reason=~"ssrf_.*"}[15m])))
```

A single stray block is expected (misconfigured source URL). Sustained >1/min indicates either a misconfigured tenant source list or an active probing attempt against the Scout worker.

### Triage

1. **Loki** — find the blocked URLs and originating tenant:
   ```logql
   {service_name="trendstorm-scout-worker"} | json
   |= "ssrf_blocked" or "SSRFBlockedError"
   | line_format "{{.tenant_id}} {{.reason}} {{.url}}"
   ```
2. Identify the dominant `reason` label. `ssrf_private_ip` and `ssrf_link_local` indicate direct RFC 1918 or AWS IMDS probing. `ssrf_internal_hostname` indicates `.internal`/`.local` hostname crafting. `ssrf_scheme_not_allowed` indicates `file://` or `ftp://` schemes — unlikely from a benign user.
3. Cross-reference `tenant_id_hash` from Prometheus with the Loki tenant_id to confirm which tenant is responsible. (Hash is `hash(tenant_id) % 100` — multiple tenants can share a bucket; use Loki for exact identification.)
4. Jaeger — search for traces tagged `trendstorm.security.ssrf_blocked` to see the full job context (category, source, job_id).
5. Check whether the blocks are clustered on a single source_id (misconfigured source) or spread across many source_ids (probing sweep).

### Immediate action

**Single-source misconfiguration**: The blocked source URL is stored in `sources` collection. Fix or remove it:
```bash
mongosh "$MONGO__URI" --eval '
  db.sources.findOne({ url: "BLOCKED_URL_HERE" })
'
```
Then update or delete the source via `DELETE /v1/sources/{id}` (requires the tenant's API key).

**Active probing / hostile tenant**: If a single tenant is generating >50 SSRF blocks/hour across many sources, suspend the tenant's API key:
```bash
mongosh "$MONGO__URI" --eval '
  db.api_keys.updateMany(
    { tenant_id: "TENANT_ID_HERE" },
    { $set: { revoked: true, revoked_at: new Date() } }
  )
'
```
This takes effect on the next request (AuthMiddleware reads key_hash live from Mongo; there is no in-process cache on the auth path).

**AWS IMDS probing** (`reason=ssrf_link_local`, IP `169.254.169.254`): Treat as high-severity — the tenant may be testing for cloud credential exfiltration. Escalate immediately per escalation path below; do not wait for the standard warn-then-page threshold.

### Escalation

- **warning (>1/min, < 3 min)**: Notify `#security-alerts` Slack channel. Monitor.
- **page (>10/min, 3 min sustained)**: Page on-call engineer (`ops@trendstorm.ai`). If AWS IMDS targeting is confirmed, escalate to `eng-leads@trendstorm.ai` within 15 minutes.
- If tenant suspension is required outside business hours, the on-call engineer has authority to revoke keys without a second approval.

### Postmortem template

```
## SSRF Block Incident — <DATE>

**Duration**: <start> – <end> (detected at <alert_fire_time>)
**Tenant(s) affected**: <list>
**Dominant reason**: <ssrf_private_ip | ssrf_link_local | ...>
**Block volume**: <N> blocks over <M> minutes
**Source URLs involved**: <count> distinct URLs

### Timeline
- HH:MM — Alert fired
- HH:MM — Triage begun (on-call: <name>)
- HH:MM — Root cause identified: <misconfiguration | probing attempt | ...>
- HH:MM — Remediation applied: <source deleted | API key revoked | ...>
- HH:MM — Block rate returned to baseline

### Root cause
<1–3 sentences>

### Was data exfiltrated?
<Yes / No / Unknown — explain>

### Remediation applied
<What changed>

### Prevention
<Rule added to global-blocklist.txt? Source validation added? Quota tightened?>

### Action items
- [ ] <owner>: <task> (due: <date>)
```

---

## 2. Prompt Injection Detected

**Alert**: `PromptInjectionDetected`
**Expression**: validator `injection_resistance` dimension score < 0.3 (emitted as a structured log field `injection_resistance_score` by `services/analysis/validator.py`)
**Severity**: page immediately — a score of 0.0 forces `passed=false` on the analysis and the validator rubric (`validator_system.md`) guarantees no injected output reaches the publisher.

The Prometheus alert uses a LogQL-derived metric (Loki ruler) rather than a direct counter, because injection scoring is a per-job float, not a bounded-cardinality counter:

```logql
# Loki ruler expression
count_over_time(
  {service_name="trendstorm-analyst-worker"}
  | json
  | injection_resistance_score < 0.3
  [5m]
) > 0
```

### Detection

1. **Loki** — query for low injection scores:
   ```logql
   {service_name="trendstorm-analyst-worker"} | json
   | injection_resistance_score < 0.5
   | line_format "job={{.job_id}} tenant={{.tenant_id}} score={{.injection_resistance_score}} notes={{.validator_notes}}"
   ```
2. Retrieve the `validator_notes` field — the validator identifies which insight or summary phrase it traced to chunk instructions. This is the key artifact for triage.
3. Retrieve the analysis from Mongo by `job_id` (the orchestrator stores it in the `analyses` collection):
   ```bash
   mongosh "$MONGO__URI" --eval '
     db.analyses.findOne({ job_id: "JOB_ID_HERE" }, { summary: 1, insights: 1 })
   '
   ```
4. Retrieve the chunks that fed the analysis (`chunks` collection, filtered by `job_id` via the documents linked to the job's category).
5. Identify which chunk(s) contained the adversarial instructions. The validator notes should reference the offending `chunk_id` values.

### Immediate action

**Score 0.0 (confirmed injection success)**:
- The orchestrator's refinement-loop logic will have already blocked publication (score=0.0 → `passed=false` → job remains in `ANALYZING` until `max_refinement_loops` exhausts, then publishes with `confidence=low`). Confirm the job did not reach `COMPLETED`:
  ```bash
  mongosh "$MONGO__URI" --eval 'db.jobs.findOne({ _id: "JOB_ID_HERE" }, { status: 1 })'
  ```
- If status is `COMPLETED`, the report may contain injected content. Retract: delete the report blob from MinIO and set job status to `FAILED` with `failure_code: "injection_retracted"`.
- Identify and quarantine the offending source. Set `source.last_fetch_status = "quarantined"` (custom status — does not break the status state machine; the Scout will skip `quarantined` sources) or delete the source.
- Delete the chunks from the `chunks` collection and evict their vectors from ChromaDB:
  ```bash
  mongosh "$MONGO__URI" --eval '
    db.chunks.deleteMany({ tenant_id: "TENANT_ID", source_id: "SOURCE_ID_HERE" })
  '
  ```
  ChromaDB eviction: use the `ChromaVectorStore.delete_by_source` method from a maintenance script (not yet in `scripts/` — add it as part of the postmortem action items).

**Score 0.1–0.3 (suspected influence, analysis not hijacked)**:
- The job likely failed validation and is in refinement loop. Monitor: if it completes with `confidence=low`, review the published report manually.
- Flag the offending chunk's source for review. Do not automatically delete; the content may be legitimate with incidental adversarial-looking phrasing.

**Systemic pattern** (multiple tenants, multiple sources, similar attack vectors):
- Check whether the attack is targeting a specific category (e.g., AI safety topic producing many scraped pages from adversarial model-spec documents). This is expected for high-stakes categories — not a platform compromise.
- If multiple tenants are affected by the same domain (the domain is weaponized to inject), add the domain to `ops/security/global-blocklist.txt` and redeploy the Scout worker (blocklist is loaded at module init; a rolling restart propagates the change within minutes).

### Escalation

- Any score < 0.3 on a published analysis: **page immediately** (`ops@trendstorm.ai`).
- If a report was published with confirmed injection content: escalate to `eng-leads@trendstorm.ai` within 30 minutes. Prepare a customer notification if the tenant is enterprise-tier.
- Multiple tenants affected simultaneously: declare a P1 security incident; engage the full on-call rotation.

### Postmortem template

```
## Prompt Injection Incident — <DATE>

**Job ID**: <ULID>
**Tenant**: <tenant_id>
**Category**: <category name>
**Injection resistance score**: <0.0 | 0.1–0.3>
**Analysis published?**: <Yes (retracted) | No (blocked by validator)>

### Attack vector
<"ignore previous instructions" | role reassignment | exfiltration request | ...>

### Offending chunk(s)
chunk_id: <ULID>
source_id: <ULID>
source_url: <URL>

### Injected instruction text (excerpted, sanitized)
<first 200 chars of the adversarial instruction, redacted if PII>

### Validator notes (verbatim)
<paste validator_notes log field>

### Timeline
- HH:MM — Analysis completed with low injection_resistance_score
- HH:MM — Alert fired / on-call paged
- HH:MM — Offending source identified
- HH:MM — Report retracted (if applicable)
- HH:MM — Source quarantined / deleted

### Was injected content published?
<Yes / No>

### Did the injection succeed in influencing analysis content?
<Yes / No / Partially — explain>

### Prevention
<New adversarial golden example added? Blocklist updated? Chunk delimiter hardened?>

### Action items
- [ ] Add golden example to eval/golden/adversarial/ for this attack vector
- [ ] <owner>: <task> (due: <date>)
```

---

## 3. PII Detection Burst

**Alert**: `PIIDetectionBurst`
**Expression**: `rate(trendstorm_security_block_total{reason=~"pii_.*"}[1m]) > 10`
**Severity**: warning at >10/min; page at >50/min sustained for 2 minutes.

A PII detection event means `DefaultPIIDetector.detect_and_redact` found a match in a chunk before it was sent to the LLM. The chunk text is stored in Mongo with the PII already redacted; the original text lives only in the raw MinIO blob. This alert fires when the rate of detections suggests a systemic data source issue rather than incidental PII in web content.

### Detection

```promql
# Rate by PII type
rate(trendstorm_security_block_total{reason=~"pii_.*"}[5m])

# Breakdown by type
sum by (reason) (rate(trendstorm_security_block_total{reason=~"pii_.*"}[15m]))
```

```logql
# Loki: find which tenants and sources are generating PII detections
{service_name="trendstorm-knowledge-worker"} | json
|= "pii_detected"
| line_format "tenant={{.tenant_id}} source={{.source_id}} types={{.pii_types}} count={{.pii_count}}"
```

High `pii_ssn` or `pii_cc` rates are higher severity than `pii_email` (emails appear in legitimate web content frequently; SSNs and credit card numbers in scraped HTML are strongly indicative of a data leak or a honeypot document).

### Triage

1. Identify the source(s) generating PII. From Loki, extract `source_id`. Look up the source URL:
   ```bash
   mongosh "$MONGO__URI" --eval 'db.sources.findOne({ _id: "SOURCE_ID_HERE" }, { url: 1, tenant_id: 1 })'
   ```
2. Fetch the raw blob from MinIO to inspect what the source actually returned (the raw HTML is at key `{tenant_id}/{job_id}/{doc_id}/raw.html`). Do NOT log the raw blob content to a shared channel if it contains real PII.
3. Determine whether the PII is:
   - **Legitimate web content with incidental PII** (e.g., a news article mentioning a phone number): normal; the redaction system is working. No action required beyond monitoring.
   - **Data breach / paste site content**: the source URL is a pastebin or breach dump. Quarantine the source; delete chunks; notify the tenant.
   - **Honeypot / test document injected by an attacker**: similar to the injection scenario above — the source is adversarial. Quarantine and follow the injection postmortem template.
4. Check whether the redacted text was sent to the LLM: the Knowledge worker calls `detect_and_redact` before `embed_batch` and before the chunk is stored in Mongo. Confirm the `chunks` collection stores the redacted version (field `text` should contain `[REDACTED:SSN]` tokens, not raw values).

### Immediate action

**Isolated, low-volume detections (email, phone)**: No action. The redaction layer is working. Close the alert if the rate drops within 5 minutes.

**SSN or credit card burst (>10 events from a single source)**:
1. Suspend the source (set `last_fetch_status = "quarantined"` in Mongo or delete it).
2. Delete all chunks produced from that source in the current job (see `chunks` deletion query above).
3. Delete the raw MinIO blob for the affected documents (`{tenant_id}/{job_id}/{doc_id}/raw.html` and `text.txt`). MinIO admin console or `mc rm` CLI.
4. Notify the tenant: their configured source returned data containing sensitive PII and has been quarantined pending their review.
5. Assess whether the raw PII was exposed to a third-party LLM (Anthropic/Gemini/OpenAI). If the Knowledge worker's PII check fired correctly, the LLM call used the redacted text and PII was NOT sent. Verify by checking the `AuditLogEntry` for the affected job:
   ```bash
   mongosh "$MONGO__URI" --eval '
     db.audit_log.find({ event_type: "pii_detected", "metadata.source_id": "SOURCE_ID_HERE" }).sort({ created_at: -1 }).limit(20)
   '
   ```
   The `audit_log` `outcome` field will be `"redacted"` if PII was caught and replaced before the LLM call.

**Audit log confirms PII was NOT redacted before an LLM call**: Treat as a data breach. Escalate to `eng-leads@trendstorm.ai` immediately. File incident with the relevant LLM provider's data-handling policy team.

### Escalation

- **>10/min, < 2 min sustained**: On-call notified via Slack. Investigate.
- **>50/min, 2 min sustained**: Page on-call. Likely a systemic source issue.
- **SSN or CC confirmed not-redacted before LLM**: P0 — data breach protocol. Engage legal within 1 hour.

### Postmortem template

```
## PII Detection Burst — <DATE>

**Duration**: <start> – <end>
**Peak rate**: <N> detections/min
**PII types detected**: <SSN | CC | EMAIL | PHONE | IBAN>
**Tenant(s) affected**: <list>
**Source URLs involved**: <list>

### Was PII sent to an external LLM?
<Yes (breach) / No (redacted) / Unknown>

### Source characterization
<Legitimate web content | Paste/breach site | Honeypot | Unknown>

### Timeline
<standard timeline>

### Redaction verified?
<Confirm audit_log outcome=redacted for all affected chunks>

### Data retention: were raw blobs deleted?
<Yes / No — explain>

### Tenant notified?
<Yes (date/time) / No — explain>

### Prevention
<Source category filtering? Blocklist update? Presidio upgrade for better recall?>

### Action items
- [ ] <owner>: <task> (due: <date>)
```

---

## 4. Unusual Audit Log Volume

**Alert**: `AuditLogVolumeSurge`
**Expression** (Loki ruler):
```logql
count_over_time(
  {service_name=~"trendstorm-.*"}
  | json
  | event_type != ""
  | tenant_id = "TENANT_ID"   # parameterized per-tenant
  [5m]
) > 1000
```

In practice, a single PromQL expression using the `audit_log` Mongo collection is not feasible — this alert uses a Loki-derived metric exported via the Loki ruler to Prometheus. The metric name is `trendstorm_audit_log_events_5m_by_tenant`. The alert fires when any single tenant exceeds 1000 audit events in a 5-minute window.

Normal baseline: < 50 audit events/5min per active tenant during a typical job (one SSRF check per source URL + one PII check per chunk). 1000 events/5min suggests either a looping job, a malicious tenant sending high-volume source lists, or an instrumentation bug.

### Detection

```logql
# Identify which tenant and event types are driving volume
{service_name=~"trendstorm-.*"} | json
| __error__="" | event_type != ""
| line_format "{{.tenant_id}} {{.event_type}} {{.outcome}}"
| count_over_time [5m]
```

```bash
# Mongo: direct audit_log count for the last 5 minutes
mongosh "$MONGO__URI" --eval '
  db.audit_log.aggregate([
    { $match: { created_at: { $gte: new Date(Date.now() - 5*60*1000) } } },
    { $group: { _id: "$tenant_id", count: { $sum: 1 } } },
    { $sort: { count: -1 } },
    { $limit: 10 }
  ])
'
```

### Triage

1. Identify the dominant `event_type` in the burst. Possibilities:
   - `ssrf_blocked` at high volume: see Section 1 (SSRF alert).
   - `pii_detected` at high volume: see Section 3 (PII alert).
   - `url_blocked` at high volume: the tenant's source list contains many entries matching the global or per-tenant blocklist. Examine the tenant's source list size:
     ```bash
     mongosh "$MONGO__URI" --eval 'db.sources.countDocuments({ tenant_id: "TENANT_ID_HERE" })'
     ```
   - `validate_url` with `outcome=allowed` at high volume: a runaway job is re-fetching sources in a loop. Check for jobs stuck in `INGESTING` with high retry count:
     ```bash
     mongosh "$MONGO__URI" --eval '
       db.jobs.find({ tenant_id: "TENANT_ID_HERE", status: "INGESTING" }, { _id: 1, created_at: 1, retry_count: 1 }).sort({ created_at: -1 })
     '
     ```
2. Check whether the audit log write rate is outpacing the Mongo write capacity. `audit_log` writes are fire-and-forget (`try/except` in the infrastructure layer) — Mongo pressure should not fail business logic, but it will produce log warnings. Check for `audit_log_write_error` log events.
3. If the volume is driven by a single job cycling through many sources rapidly: the Scout worker's asyncio.Queue producer-consumer is working as designed, but the tenant may have an unusually large source list (thousands of URLs). This is not inherently malicious. Verify the tenant's source count is within plan limits.

### Immediate action

**Runaway job loop**: Terminate the job:
```bash
mongosh "$MONGO__URI" --eval '
  db.jobs.updateOne(
    { _id: "JOB_ID_HERE" },
    { $set: { status: "FAILED", failure_code: "manual_termination_audit_surge", updated_at: new Date() } }
  )
'
```

**Tenant with oversized source list causing legitimate volume**: No immediate action. Consider applying a per-tenant source count limit via `BusinessRuleError(code="source_limit_exceeded")` (not yet implemented — add as a postmortem action item).

**Instrumentation loop** (the same event being written multiple times per action): This is a code bug. Identify the offending log site from Loki, file a bug, and temporarily raise the alert threshold to avoid noise while the fix is deployed.

**Audit log collection growth**: At the current 365-day TTL, a tenant generating 200 events/job at 100 jobs/day produces ~7.3M entries/year. The `audit_log__ttl_created` index will expire them. If collection size is unexpectedly large, verify the TTL index is present:
```bash
mongosh "$MONGO__URI" --eval 'db.audit_log.getIndexes()'
```
The index named `audit_log__ttl_created` must be present with `expireAfterSeconds: 31536000`.

### Escalation

- **1000–5000 events/5min**: On-call investigates. No page unless root cause is a security event (escalate per the relevant sub-section above).
- **>5000 events/5min**: Page on-call — Mongo write pressure is now a platform concern, independent of the security cause.
- **Audit log data corruption** (entries missing required fields): Investigate `AuditLogEntry` model validation — `extra="forbid"` will cause silent drops if a field is unexpected. File a bug immediately; the audit log is a regulatory artifact.

### Postmortem template

```
## Audit Log Volume Surge — <DATE>

**Duration**: <start> – <end>
**Peak volume**: <N> events/5min
**Tenant**: <tenant_id>
**Dominant event_type**: <ssrf_blocked | pii_detected | url_blocked | validate_url | ...>

### Root cause
<Runaway job | Oversized source list | Instrumentation bug | Security event>

### Was Mongo write capacity impacted?
<Yes / No>

### Timeline
<standard>

### Remediation applied
<Job terminated | Source list trimmed | Alert threshold adjusted | ...>

### Prevention
<Source count limit enforced? Per-tenant audit rate limit? TTL index verified?>

### Action items
- [ ] <owner>: <task> (due: <date>)
```
