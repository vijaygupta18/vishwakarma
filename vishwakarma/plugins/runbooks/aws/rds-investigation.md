# RDS Investigation Runbook

## Goal
Investigate AWS RDS CPU/connection/memory alerts. Determine:
1. Which instance is affected (including autoscaled replicas)
2. What is causing it (top SQL, wait events, locks, autovacuum)
3. Whether it is causing user-facing impact (5xx, latency, business metric degradation)
4. Confidence level in the root cause
5. Whether an immediate fix is needed or it will self-resolve

**Agent Mandate:** Read-only. Do not modify any DB settings, kill queries, or change instance configurations.

## Time Window
- Use `startsAt` from the alert as your investigation anchor
- Query window: `startsAt - 10 minutes` to `startsAt + 1 hour`
- If `startsAt` not available, use `now - 30 minutes`

## Infrastructure Reference
Refer to the **Site Knowledge Base** for your cluster's specific values:
- Aurora cluster identifiers (used to discover all instances dynamically)
- Alert name → cluster mapping
- Elasticsearch endpoint + app log index name
- Service → DB mapping (which app connects to which DB)
- Business-critical Prometheus metrics

---

## IMPORTANT: Tool Routing
- **RDS metrics (CPU, connections, IOPS, memory)**: Use `aws cloudwatch get-metric-statistics` via bash — NOT prometheus. RDS metrics are NOT in Prometheus.
- **Instance discovery**: Use `aws rds describe-db-instances` to find ALL instances including autoscaled replicas — NEVER rely on hardcoded instance lists.
- **Performance Insights**: Use `aws pi describe-dimension-keys` — requires `DbiResourceId` (not DBInstanceIdentifier). Get it from `describe-db-instances`.
- **Business impact (5xx, latency)**: Use `prometheus_query_range` — these ARE application metrics in Prometheus.
- **Application/DB logs**: Use `elasticsearch_search`
- **Direct SQL diagnostics**: Use `db_query(bap_pg)` or `db_query(bpp_pg)` for pg_stat_activity

---

## Step 0: Alert Freshness Check — Is This Real?

Before investigating, determine if this is a genuine ongoing issue, a resolved transient spike, or a stale/duplicate alert.

**Check current metric value RIGHT NOW** on the instance from the alert (if identifiable):
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
  --dimensions Name=DBInstanceIdentifier,Value=<instance> \
  --start-time $(date -u -d '5 minutes ago' +%Y-%m-%dT%H:%M:%SZ) --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
  --period 60 --statistics Average Maximum --region <region> --output json
```

**Interpret:**
- **Current CPU > threshold** → GENUINE, ONGOING — investigate urgently, full depth
- **Current CPU normal, startsAt < 30 min ago** → RESOLVED, TRANSIENT — still investigate but note self-recovery
- **Current CPU normal, startsAt > 2 hours ago** → STALE ALERT — note "alert is stale, issue resolved X hours ago" and do a lighter investigation
- **Alert fingerprint matches a recent investigation** → DUPLICATE — skip

**Include this assessment in your RCA under "Alert Assessment".**

---

## Step 1: Discover ALL Instances + Identify the Affected One

**CRITICAL: Aurora clusters can have autoscaled read replicas that don't appear in any static list. Always discover dynamically.**

Run all of these in parallel:

**1a — Discover all instances in the Aurora cluster:**
```
aws rds describe-db-instances --region <region> \
  --query 'DBInstances[?DBClusterIdentifier==`<cluster-id>`].[DBInstanceIdentifier,DbiResourceId,DBInstanceClass,DBInstanceStatus]' \
  --output table
```

**1b — Identify writer vs readers:**
```
aws rds describe-db-clusters --db-cluster-identifier <cluster-id> --region <region> \
  --query 'DBClusters[0].DBClusterMembers[*].[DBInstanceIdentifier,IsClusterWriter]' --output table
```

**1c — CPU across ALL discovered instances (include every instance from 1a):**
```
for instance in <all-discovered-instances>; do
  echo "=== $instance ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
    --dimensions Name=DBInstanceIdentifier,Value=$instance \
    --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
    --period 60 --statistics Average Maximum --region <region> --output json 2>/dev/null \
    | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average | floor)%, max=\(.Maximum | floor)%"'
done
```

**1d — Look up the alarm (works regardless of current state):**
```
aws cloudwatch describe-alarms --alarm-names "<alarm-name-from-alert>" --region <region> \
  --query 'MetricAlarms[*].[AlarmName,Dimensions,StateReason,StateUpdatedTimestamp,StateValue]' --output table
```

**1e — Alarm history to detect flapping/recurring pattern:**
```
aws cloudwatch describe-alarm-history --alarm-name "<alarm-name-from-alert>" --region <region> \
  --start-date <startsAt-2h ISO8601> --end-date <startsAt+1h ISO8601> \
  --history-item-type StateUpdate \
  --query 'AlarmHistoryItems[].[AlarmName,Timestamp,HistorySummary]' --output table
```

**1f — Recent deploys (did code change recently?):**
```
kubectl get replicasets -n <namespace> --sort-by=.metadata.creationTimestamp -o wide | tail -15
```

**1g — Aurora events (scaling, failover, maintenance):**
```
aws rds describe-events --source-type db-instance --duration 120 --region <region> \
  --query 'Events[].[SourceIdentifier,Date,Message]' --output table
```

**After Step 1:** Identify the **highest-CPU instance** — this is the target for all subsequent steps. Note whether it's writer or reader, and whether it's a static or autoscaled instance. Also note if ALL instances spiked simultaneously (points to traffic surge or external dependency, not a single bad query).

---

## Step 2: Characterize the Spike — Metrics Deep Dive

Run all of these in parallel on the **highest-CPU instance**:

**2a — Related metrics (each as a separate command for parallelism):**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections \
  --dimensions Name=DBInstanceIdentifier,Value=<target-instance> \
  --start-time <startsAt-10min> --end-time <startsAt+1h> \
  --period 60 --statistics Average Maximum --region <region> --output json \
  | jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average|floor) max=\(.Maximum|floor)"'
```
Repeat for: `ReadIOPS`, `WriteIOPS`, `FreeableMemory`, `ReadLatency`, `WriteLatency`, `ReplicationLag` (readers only).

**2b — 7-day baseline comparison (MANDATORY — "high" is meaningless without a baseline):**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
  --dimensions Name=DBInstanceIdentifier,Value=<target-instance> \
  --start-time <startsAt-7days-15min> --end-time <startsAt-7days+1h> \
  --period 300 --statistics Average Maximum --region <region> --output json \
  | jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average|floor)% max=\(.Maximum|floor)%"'
```
Do the same for `ReadIOPS` and `DatabaseConnections`. If current values are within 20% of 7-day-ago values → this is normal load, not an incident.

**Characterization matrix (fill this out after Step 2):**
```
Instance: <name> (<writer/reader>, <instance-class>)
CPU:         <current>% (baseline: <7d-ago>%)
ReadIOPS:    <current> (baseline: <7d-ago>)
WriteIOPS:   <current> (baseline: <7d-ago>)
Connections: <current> (baseline: <7d-ago>)
FreeMemory:  <current MB> (baseline: <7d-ago MB>)
ReplicaLag:  <if reader>
Pattern:     SPIKE / GRADUAL / STEP / NORMAL
```

---

## Step 3: Find the Culprit Query — Performance Insights

Use the `DbiResourceId` from Step 1a (e.g. `db-ABCDEF123456`), NOT the DBInstanceIdentifier.

Run these in parallel:

**3a — Top SQL by load:**
```
aws pi describe-dimension-keys \
  --service-type RDS --identifier <DbiResourceId-of-target> \
  --start-time <startsAt-10min> --end-time <startsAt+1h> \
  --metric db.load.avg \
  --group-by '{"Group":"db.sql_tokenized","Limit":10}' \
  --region <region> \
  | jq -r '.Keys[]|"load=\(.Total): \(.Dimensions."db.sql_tokenized.statement")"'
```

**3b — Wait events (what the DB is spending time on):**
```
aws pi describe-dimension-keys \
  --service-type RDS --identifier <DbiResourceId-of-target> \
  --start-time <startsAt-10min> --end-time <startsAt+1h> \
  --metric db.load.avg \
  --group-by '{"Group":"db.wait_event","Limit":10}' \
  --region <region> \
  | jq -r '.Keys[]|"load=\(.Total): \(.Dimensions."db.wait_event.name")"'
```

**3c — Top SQL on the writer too (if target is a reader):**
If the highest-CPU instance is a reader, also run 3a on the writer — heavy writes on the writer cause replication load on readers.

**Interpret wait events:**
| Wait Event | Meaning | Likely Cause |
|---|---|---|
| `IO:DataFileRead` dominant | Full table scans | Missing index or bloated table |
| `IO:XactSync` + `Timeout:VacuumDelay` | Write + vacuum | Autovacuum running on large table |
| `Lock:relation` or `Lock:tuple` | Lock contention | DDL or long-running transaction holding locks |
| `CPU` dominant (>50% of load) | Pure compute | Complex query, JSON parsing, regex in WHERE |
| `LWLock:BufferMapping` | Buffer pool contention | Working set exceeds shared_buffers |
| `Client:ClientRead` | Waiting for app | App is slow consuming results, connection pool issue |

**If PI returns empty Keys after 2 attempts with different time windows:**
```
aws rds describe-db-log-files --db-instance-identifier <instance-id> --region <region>
aws rds download-db-log-file-portion --db-instance-identifier <instance-id> \
  --log-file-name <most-recent-slow-query-log> --region <region>
```

---

## Step 4: Direct SQL Diagnostics (if database toolset is enabled)

Run `learnings_read(database)` first to get the PostgreSQL diagnostic query templates.

**Run all of these in parallel on the affected DB's PG connection (bap_pg for customer, bpp_pg for driver):**

**4a — Active queries consuming CPU:**
```
db_query(<connection>, "SELECT pid, now() - query_start AS duration, state, wait_event_type, wait_event, left(query, 200) as query FROM pg_stat_activity WHERE state = 'active' AND query NOT LIKE '%pg_stat_activity%' ORDER BY duration DESC LIMIT 20")
```

**4b — Long-running queries (stuck queries):**
```
db_query(<connection>, "SELECT pid, now() - query_start AS duration, state, wait_event_type, wait_event, left(query, 200) as query FROM pg_stat_activity WHERE state = 'active' AND now() - query_start > interval '5 seconds' ORDER BY duration DESC LIMIT 20")
```

**4c — Connection count by application (pool exhaustion check):**
```
db_query(<connection>, "SELECT application_name, state, count(*) FROM pg_stat_activity GROUP BY application_name, state ORDER BY count DESC LIMIT 30")
```

**4d — Tables with sequential scans (missing index check):**
```
db_query(<connection>, "SELECT relname, seq_scan, idx_scan, seq_tup_read, CASE WHEN seq_scan > 0 THEN round(seq_tup_read::numeric / seq_scan) ELSE 0 END AS avg_rows_per_scan FROM pg_stat_user_tables WHERE seq_scan > 100 AND seq_tup_read > 100000 ORDER BY seq_tup_read DESC LIMIT 20")
```

**4e — Lock contention:**
```
db_query(<connection>, "SELECT blocked.pid, left(blocked.query, 100) as blocked_query, blocking.pid as blocking_pid, left(blocking.query, 100) as blocking_query, now() - blocked.query_start AS blocked_duration FROM pg_stat_activity blocked JOIN pg_locks bl ON bl.pid = blocked.pid AND NOT bl.granted JOIN pg_locks kl ON kl.locktype = bl.locktype AND kl.database = bl.database AND kl.relation = bl.relation AND kl.page = bl.page AND kl.tuple = bl.tuple AND kl.pid != bl.pid AND kl.granted JOIN pg_stat_activity blocking ON blocking.pid = kl.pid LIMIT 10")
```

**4f — Table bloat (autovacuum check):**
```
db_query(<connection>, "SELECT relname, n_live_tup, n_dead_tup, round(n_dead_tup::numeric / greatest(n_live_tup, 1) * 100, 1) AS dead_pct, last_autovacuum, last_autoanalyze FROM pg_stat_user_tables WHERE n_dead_tup > 100000 ORDER BY n_dead_tup DESC LIMIT 15")
```

**4g — Currently running autovacuum:**
```
db_query(<connection>, "SELECT pid, now() - query_start AS duration, left(query, 150) as query FROM pg_stat_activity WHERE query LIKE 'autovacuum:%' ORDER BY duration DESC")
```

---

## Step 4B: Deep Query Analysis (ALWAYS DO THIS when PI identifies a culprit query)

When Performance Insights (Step 3) identifies a high-load query, you MUST dig into **why** that query is slow. Don't just report "query X is consuming 40% load" — find out if it's missing an index, doing a sequential scan, or hitting a bloated table.

**4B-a — EXPLAIN ANALYZE the culprit query:**
Take the tokenized SQL from PI (Step 3a), fill in reasonable parameter values, and run:
```
db_query(<connection>, "EXPLAIN (ANALYZE, BUFFERS, FORMAT TEXT) <the-culprit-query-with-sample-params>")
```
Look for:
- `Seq Scan` on large tables → missing index
- `Rows Removed by Filter:` huge number → index exists but doesn't cover the WHERE clause
- `Sort` with `external merge Disk` → not enough work_mem, spilling to disk
- `Nested Loop` with high actual rows vs estimated → planner misestimate, needs ANALYZE
- `Buffers: shared read` very high → cold cache, data not in shared_buffers

**4B-b — Check indexes on the culprit table:**
```
db_query(<connection>, "SELECT indexname, indexdef FROM pg_indexes WHERE tablename = '<table-from-culprit-query>' ORDER BY indexname")
```

**4B-c — Check table size and row count:**
```
db_query(<connection>, "SELECT relname, pg_size_pretty(pg_total_relation_size(oid)) as total_size, pg_size_pretty(pg_relation_size(oid)) as table_size, pg_size_pretty(pg_indexes_size(oid)) as index_size, reltuples::bigint as estimated_rows FROM pg_class WHERE relname = '<table-from-culprit-query>'")
```

**4B-d — Check if table stats are stale (planner may be using wrong estimates):**
```
db_query(<connection>, "SELECT relname, last_analyze, last_autoanalyze, n_live_tup, n_dead_tup FROM pg_stat_user_tables WHERE relname = '<table-from-culprit-query>'")
```

**4B-e — Check column statistics for WHERE clause columns:**
```
db_query(<connection>, "SELECT attname, n_distinct, most_common_vals, most_common_freqs, correlation FROM pg_stats WHERE tablename = '<table-from-culprit-query>' AND attname IN ('<where-column-1>', '<where-column-2>')")
```

**Interpretation:**
- If `Seq Scan` + no matching index for the WHERE clause → **Missing index**. Report which columns need indexing.
- If index exists but `Seq Scan` still used → Table stats may be stale (run ANALYZE), or query planner estimated index scan as more expensive (check `n_distinct`, `correlation`).
- If `Index Scan` but still slow → Index is there but query returns too many rows, or table is extremely large. Check if a composite index would help.
- If `Rows Removed by Filter` >> actual rows → The index doesn't cover the filter. A more selective index is needed.

---

## Step 5: Check Business Impact (Run in Parallel with Steps 3-4)

**5a — 5xx error rate (Prometheus):**
- query: `sum by(service,handler)(rate(http_request_duration_seconds_count{handler!="/v1/",status_code=~"^5.."}[1m]))`
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**5b — P99 latency (Prometheus):**
- query: `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))`
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**5c — ALB 5xx from CloudWatch (fallback if Prometheus unavailable):**
```
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB \
  --metric-name HTTPCode_Target_5XX_Count \
  --dimensions Name=LoadBalancer,Value=<alb-arn-from-knowledge-base> \
  --start-time <startsAt-10min> --end-time <startsAt+1h> \
  --period 60 --statistics Sum --region <region> --output json \
  | jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): \(.Sum) 5xx"'
```

**5d — ALB response time:**
```
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB \
  --metric-name TargetResponseTime \
  --dimensions Name=LoadBalancer,Value=<alb-arn-from-knowledge-base> \
  --start-time <startsAt-10min> --end-time <startsAt+1h> \
  --period 60 --statistics Average p99 --region <region> --output json \
  | jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average)s"'
```

**5e — Key business metrics (check Site Knowledge Base for deployment-specific metrics):**
- query: use business-critical metrics from knowledge base (e.g., conversion rates, transaction counts)
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `5m`

**Impact assessment — fill this out:**
```
5xx rate:    NONE / LOW (<10/min) / MEDIUM (10-100/min) / HIGH (>100/min)
P99 latency: NORMAL / DEGRADED (>3s) / SEVERE (>10s)
ALB 5xx:     <count per minute>
Business:    STABLE / DEGRADED — <which metrics affected>
User impact: YES / NO — <describe affected user operations>
```

---

## Step 6: Correlate — Application-Side Evidence

**ONLY run this step if Steps 1-5 did NOT give a clear root cause.** Skip if you already identified the culprit.

**6a — App-side DB errors from services connected to this DB:**
```
kubectl logs -n <namespace> -l app=<service-name> --since=15m --tail=200 2>/dev/null \
  | grep -iE 'connection|timeout|refused|deadlock|pool|too many|query' | head -30
```

**6b — Elasticsearch search for DB errors:**
Use `elasticsearch_search` tool:
```json
{
  "index": "<app-log-index-from-knowledge-base>",
  "size": 20,
  "sort": [{"@timestamp": "desc"}],
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "<startsAt-10min>", "lte": "<startsAt+1h>"}}},
        {"bool": {
          "should": [
            {"match": {"message": "connection refused"}},
            {"match": {"message": "too many connections"}},
            {"match": {"message": "deadlock"}},
            {"match": {"message": "query timeout"}},
            {"match": {"message": "connection pool"}},
            {"match": {"message": "statement timeout"}},
            {"match": {"message": "canceling statement"}}
          ],
          "minimum_should_match": 1
        }}
      ]
    }
  },
  "_source": ["message", "@timestamp", "service"]
}
```

**6c — Check if errors are NEW or pre-existing:**
Run the same ES query for **yesterday's same time window**. If the same errors appear yesterday → pre-existing, NOT caused by this incident.

---

## Synthesis — Hypothesis Verification Matrix

**MANDATORY: Work through EVERY hypothesis below. For each one, state CONFIRMED / RULED OUT / INCONCLUSIVE with specific evidence.**

### Hypothesis 1: Missing Index / Full Table Scan
**Check:** PI top SQL shows one query > 40% db.load + ReadIOPS spike > 5x baseline
**Verify:** The query's table has high `seq_scan` in `pg_stat_user_tables` + low `idx_scan`
**Rule out:** If ReadIOPS is normal and no single query dominates → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 2: Bad Deploy / New Expensive Query
**Check:** PI top SQL shows a query pattern not seen in 7-day baseline + CPU spike correlates with deploy time
**Verify:** `kubectl get replicasets -n <ns> --sort-by=.metadata.creationTimestamp` shows deploy within 30min of spike
**Rule out:** If no deploy happened recently AND top queries are known patterns → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 3: Autovacuum
**Check:** PI wait events show `Timeout:VacuumDelay` or `IO:XactSync` dominant + WriteIOPS high
**Verify:** `pg_stat_activity` shows `autovacuum:` query running + `pg_stat_user_tables` shows high `n_dead_tup` on affected table
**Rule out:** If no autovacuum in pg_stat_activity AND VacuumDelay not in wait events → NOT this
**Self-resolves:** YES — autovacuum will complete. Note estimated time based on dead tuple count.
**Confidence if confirmed:** HIGH

### Hypothesis 4: Connection Pool Exhaustion
**Check:** DatabaseConnections surged > 2x baseline + app logs show "too many clients" / "connection pool" / "could not obtain connection"
**Verify:** `pg_stat_activity` grouped by `application_name` shows one app dominating connections
**Rule out:** If connections are normal and no pool errors in logs → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 5: Lock Contention
**Check:** PI wait events show `Lock:relation` or `Lock:tuple` + pg_locks query shows blocked queries
**Verify:** Identify the blocking query and how long it's been holding locks
**Rule out:** If no lock-related wait events AND pg_locks shows no contention → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 6: Replication Lag (reader only)
**Check:** Target is a reader + writer WriteIOPS is high + ReplicationLag metric is increasing
**Verify:** Reader CPU correlates with writer write activity, not reader's own queries
**Rule out:** If target is the writer OR ReplicationLag is < 1s → NOT this
**Confidence if confirmed:** HIGH if lag > 30s

### Hypothesis 7: Memory Pressure
**Check:** FreeableMemory dropped > 50% from baseline + swap activity
**Verify:** If FreeableMemory < 500MB, instance is swapping → CPU spent on I/O, not queries
**Rule out:** If FreeableMemory is > 2GB and stable → NOT this
**Confidence if confirmed:** MEDIUM

### Hypothesis 8: Background Job / Analytics Query
**Check:** CPU high + no 5xx + no business impact + PI shows batch/analytics query
**Verify:** The heavy query has identifiable batch pattern (large table scan, aggregation, COPY, pg_dump)
**Rule out:** If there IS 5xx or business impact → NOT just a background job
**Confidence if confirmed:** MEDIUM (low urgency)

### Hypothesis 9: Traffic Surge (ALL instances spike together)
**Check:** CPU spiked on ALL instances simultaneously (writer + all readers) + ReadIOPS and connections surged across the board
**Verify:** Prometheus shows incoming request rate spike at the same time: `sum(rate(http_request_duration_seconds_count[1m])) by (service)`
**Rule out:** If only one instance spiked → NOT traffic surge, it's a query/instance-specific issue
**Confidence if confirmed:** HIGH
**Fix:** Auto-scaling should handle it. If it didn't trigger, check Aurora auto-scaling policy thresholds.

### Hypothesis 10: CPU Burst Credit Exhaustion (t-class / serverless instances only)
**Check:** Instance class is `db.t*` or `db.serverless` + `CPUCreditBalance` dropped to 0
**Verify:**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUCreditBalance \
  --dimensions Name=DBInstanceIdentifier,Value=<target-instance> \
  --start-time <startsAt-2h> --end-time <startsAt+1h> \
  --period 300 --statistics Average --region <region> --output json
```
If credits hit 0 → CPU was throttled to baseline performance.
**Rule out:** If instance is `db.r*` or `db.m*` (these don't have burst credits) → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 11: Parameter Group / Configuration Change
**Check:** `aws rds describe-events` shows a parameter group change or instance modification around the spike time
**Verify:**
```
aws rds describe-events --source-type db-instance --source-identifier <target-instance> \
  --duration 1440 --region <region> \
  --query 'Events[].[Date,Message]' --output table
```
Look for: "Applied parameter group", "Modified", "Rebooted"
**Rule out:** If no events in the last 24h → NOT this
**Confidence if confirmed:** HIGH

### Hypothesis 12: Normal Load / False Alarm
**Check:** Current CPU is within 20% of 7-day baseline + no anomaly in any metric
**Verify:** Alarm threshold may be set too low for this instance's normal load
**Rule out:** If CPU is genuinely > 2x baseline → this IS an anomaly
**Confidence if confirmed:** HIGH (no action needed)
**Fix:** Adjust alarm threshold to match normal load pattern.

---

## Final Verdict

After verifying all hypotheses, state:

```
## Verified Hypotheses
| # | Hypothesis | Verdict | Key Evidence |
|---|-----------|---------|--------------|
| 1 | Missing index / full table scan | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 2 | Bad deploy / new query | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 3 | Autovacuum | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 4 | Connection pool exhaustion | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 5 | Lock contention | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 6 | Replication lag | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 7 | Memory pressure | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 8 | Background job / analytics | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 9 | Traffic surge (all instances) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 10 | CPU burst credit exhaustion | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 11 | Parameter group change | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 12 | Normal load / false alarm | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |

## Root Cause
<Confirmed hypothesis with full evidence chain>

## Confidence: HIGH / MEDIUM / LOW
<Why this confidence level — what evidence supports it, what's missing>

## Business Impact
5xx: <rate and trend>
Latency: <p99 and trend>
Users affected: <yes/no, which operations>

## Immediate Fix
<Exact action needed, or "No action needed — self-resolving" with estimated time>
- If missing index: "CREATE INDEX CONCURRENTLY idx_<table>_<column> ON <table>(<column>)" — requires DBA approval
- If bad deploy: "Rollback deployment <name> to previous revision: kubectl rollout undo deployment/<name> -n <ns>"
- If autovacuum: "No action — autovacuum will complete in ~X minutes. Monitor CPU."
- If connection pool: "Scale down <service> HPA / restart pods to release connections"
- If lock contention: "Identify and terminate blocking PID <pid>: SELECT pg_terminate_backend(<pid>)" — requires DBA approval
- If replication lag: "No immediate fix — reduce write load or add reader capacity. Monitor lag."
- If memory pressure: "Consider instance class upgrade from <current> to <recommended>"
- If background job: "No urgent action — schedule batch jobs during off-peak hours"
- If traffic surge: "Verify Aurora auto-scaling triggered. If not, manually add read replica."
- If burst credit exhaustion: "Upgrade from <t-class> to <r-class> instance — t-class is not suitable for sustained load"
- If parameter change: "Revert parameter group change or reboot instance to apply fix"
- If normal load: "Adjust alarm threshold from <current> to <recommended>"

## Prevention
<What change prevents recurrence — be specific>

## Needs More Investigation
YES / NO — <if YES, what specifically needs checking and by whom>
```

---

## Extended Investigation

If ALL hypotheses are INCONCLUSIVE after the above steps:
- Correlate timestamps across ALL sources: metrics spike, log errors, pod restarts, deploys, external events
- Check upstream/downstream services this DB depends on
- Look for scheduled jobs (cron, batch) running at the incident time
- Check `kubectl get events -n <namespace>` for pod restarts or node pressure
- Check if an Aurora scaling event occurred: `aws rds describe-events --source-type db-instance --region <region>`
- Check if a parameter group change was applied: `aws rds describe-db-parameters --db-parameter-group-name <group> --region <region>`
