# RDS Investigation Runbook

## Goal
Investigate AWS RDS CPU/connection/memory alerts. Find:
1. Which instance is affected and what metric is high
2. Which query or operation is causing it (top SQL from Performance Insights)
3. Whether there is business impact (5xx errors, ride-to-search ratio drop)

**Agent Mandate:** Read-only. Do not modify any DB settings.

## Time Window
- Use `startsAt` from the alert as your investigation anchor.
- Query window: `startsAt - 10 minutes` to `startsAt + 1 hour`
- If `startsAt` not available, use `now - 30 minutes`.

## Infrastructure Reference
Refer to the **Site Knowledge Base** for your cluster's specific values:
- RDS instance identifiers + roles (writer/reader) per cluster
- Alert name → instance mapping (alarm names often don't match instance IDs)
- Elasticsearch endpoint + app log index name
- Service → RDS mapping (which app connects to which DB)

---

## Step 1: Identify the Affected Instance and Metric

**IMPORTANT: Alarms often resolve before investigation starts. If the alarm state is OK, that is normal — proceed using `startsAt` as your time anchor. Do NOT retry describe-alarms.**

Run all of these in parallel:

**1a — Look up the alarm (works regardless of current state):**
```
aws cloudwatch describe-alarms --alarm-names "<alarm-name-from-alert>" --region <region> \
  --query 'MetricAlarms[*].[AlarmName,Dimensions,StateReason,StateUpdatedTimestamp,StateValue]' --output table
```

**1b — Check alarm history to detect flapping/recurring pattern:**
```
aws cloudwatch describe-alarm-history --alarm-name "<alarm-name-from-alert>" --region <region> \
  --start-date <startsAt-2h ISO8601> --end-date <startsAt+1h ISO8601> \
  --history-item-type StateUpdate \
  --query 'AlarmHistoryItems[].[AlarmName,Timestamp,HistorySummary]' --output table
```

**1c — CPU across all instances in the cluster (use instance IDs from knowledge base):**
```
for instance in <instance-1> <instance-2> <instance-3>; do
  echo "=== $instance ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
    --dimensions Name=DBInstanceIdentifier,Value=$instance \
    --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
    --period 60 --statistics Average Maximum --region <region> --output json 2>/dev/null \
    | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average | floor)%, max=\(.Maximum | floor)%"'
done
```

**IMPORTANT: Always check all instances in the cluster — alarm names often don't match the actual affected instance. The instance with highest CPU is the one to investigate.**

**1d — Related metrics on the highest-CPU instance (run in parallel with 1c):**
```
for metric in DatabaseConnections ReadIOPS WriteIOPS FreeableMemory; do
  echo "=== $metric ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name $metric \
    --dimensions Name=DBInstanceIdentifier,Value=<highest-cpu-instance> \
    --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
    --period 60 --statistics Average Maximum --region <region> --output json 2>/dev/null \
    | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): \(.Average // .Maximum | floor)"'
done
```

---

## Step 2: Find the Top Queries Using Performance Insights

**IMPORTANT:** PI requires `DbiResourceId` (e.g. `db-ABCDEF123456`), NOT the DBInstanceIdentifier. Fetch it first.

**2a — Get DbiResourceId and query PI in one shot:**
```
DBI=$(aws rds describe-db-instances --db-instance-identifier <instance-id> --region <region> \
  --query 'DBInstances[0].DbiResourceId' --output text)

aws pi describe-dimension-keys \
  --service-type RDS --identifier $DBI \
  --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
  --metric db.load.avg \
  --group-by '{"Group":"db.sql_tokenized","Limit":10}' \
  --region <region> 2>/dev/null \
  | jq -r '.Keys[] | "load=\(.Total): \(.Dimensions."db.sql_tokenized.statement")"'
```

**2b — Check wait events in parallel (confirms autovacuum, lock contention, I/O):**
```
aws pi describe-dimension-keys \
  --service-type RDS --identifier $DBI \
  --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
  --metric db.load.avg \
  --group-by '{"Group":"db.wait_event","Limit":10}' \
  --region <region> 2>/dev/null \
  | jq -r '.Keys[] | "load=\(.Total): \(.Dimensions."db.wait_event.name")"'
```

Look for:
- `IO:DataFileRead` dominant → full table scan or missing index
- `IO:XactSync` + `Timeout:VacuumDelay` → autovacuum
- `Lock:relation` or `Lock:tuple` → lock contention
- Top SQL > 40% db.load → single query is the culprit

**If PI returns empty Keys after 2 attempts with different time windows → move to slow query logs:**
```
aws rds describe-db-log-files --db-instance-identifier <instance-id> --region <region>
aws rds download-db-log-file-portion --db-instance-identifier <instance-id> \
  --log-file-name <most-recent-slow-query-log> --region <region>
```

---

## Step 3: Check Business Impact (Run in Parallel)

**Use `prometheus_query_range` tool for all PromQL. Do NOT use http_get.**

Run all three simultaneously:

**3a — 5xx error rate:**
- query: `sum by(service,handler)(rate(http_request_duration_seconds_count{handler!="/v1/",status_code=~"^5.."}[1m]))`
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**3b — P99 latency:**
- query: `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))`
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**3c — Ride-to-search ratio (are riders getting matched?):**
- query: use ride-created and search-request metrics from knowledge base
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `5m`

**Fallback if Prometheus returns no data:**
```
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB \
  --metric-name HTTPCode_Target_5XX_Count \
  --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
  --period 300 --statistics Sum --region <region>
```

---

## Step 4: Correlate — Which Service is Hitting This DB?

Only run this step if Step 3 shows user impact OR connections are high.

**4a — Live pod logs for DB errors (use service names from knowledge base):**
```
timeout 30 stern -n <namespace> <service-name> --since 1h 2>/dev/null \
  | grep -iE "connection|timeout|refused|deadlock|pool" | head -50
```

**4b — Elasticsearch search for DB errors:**
Use `elasticsearch_search` tool with index from knowledge base:
```json
{
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
            {"match": {"message": "connection pool"}}
          ],
          "minimum_should_match": 1
        }}
      ]
    }
  },
  "_source": ["message", "@timestamp", "service"]
}
```

---

## Synthesis — Decision Tree

Work through top-to-bottom. Stop at first match.

**1. CPU high + ReadIOPS > 5x normal?**
→ **Missing index or full table scan** — PI top SQL will show it. Confidence: HIGH if one query > 40% db.load.

**2. CPU high + connections surged + coincides with deploy?**
→ **New code introduced expensive query or connection leak** — check deploy time vs spike time (must overlap within 10 min). Confidence: HIGH if deploy time matches exactly.

**3. CPU high + WriteIOPS high + no recent deploy?**
→ **Autovacuum or bulk insert/update** — PI wait events show `IO:XactSync` + `Timeout:VacuumDelay`. Confidence: MEDIUM.

**4. Connections high + DB errors in app logs?**
→ **PgBouncer pool exhaustion** — app logs show "too many clients" or "connection pool". Confidence: HIGH.

**5. Replication lag high + WriteIOPS high on writer?**
→ **Heavy write load causing replica lag** — readers falling behind, reads hitting writer. Confidence: HIGH if lag > 30s.

**6. CPU high + no 5xx + no ride-to-search drop?**
→ **Background job or analytics query — lower urgency.** No immediate action needed. PI will show who is running it.

**7. None match?** → Continue with Extended Investigation.

**After choosing hypothesis:** run adversarial check — try to find evidence that contradicts it before concluding.

---

## Step 5: Direct SQL Diagnostics (if database toolset is enabled)

If the `database` toolset is available, run `learnings_read(database)` to load PostgreSQL diagnostic query templates, then query the database directly:

**5a — Active/stuck queries consuming CPU:**
Use `db_query` against the appropriate PostgreSQL connection to check `pg_stat_activity` for long-running queries, connection counts by application, and lock contention. The exact queries are in the database learnings.

**5b — Missing indexes causing sequential scans:**
Check `pg_stat_user_tables` for tables with high sequential scan counts vs low index scan counts. This is the most common cause of RDS CPU spikes.

**5c — Table bloat:**
Check `pg_stat_user_tables` for tables with high dead tuple counts that need vacuuming.

---

## Extended Investigation

If root cause is still not confirmed with HIGH or MEDIUM confidence:
- Correlate timestamps: metrics spike, log errors, pod restarts, recent deploys
- Check upstream/downstream services this DB depends on
- Look for scheduled jobs (cron, batch) running at the incident time
- Check `kubectl get events -n <namespace>` for pod restarts or node pressure around the incident time
