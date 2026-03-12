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
- **Prometheus:** `http://<prometheus-url>`
- **Elasticsearch app logs index:** `app-logs-YYYY-MM-DD`
- **Elasticsearch endpoint:** `https://<elasticsearch-endpoint>`
- **Known RDS instances:** `bap-reader-1`, `bap-writer-1`, `bap-reader-3`, `bpp-writer-1`, `bpp-reader-1`, `utils-db-instance`, `utils-db`

---

## Step 1: Identify the Affected Instance and Metric

**IMPORTANT: Alarms often resolve before investigation starts. If `--state-value ALARM` returns empty, the alarm has already cleared — this is normal and expected. DO NOT retry this command. Proceed using `startsAt` from the alert.**

If the alert payload contains an alarm name, look it up directly (works regardless of current state):
```
aws cloudwatch describe-alarms --alarm-names "<alarm-name-from-alert>" --region <region> \
  --query 'MetricAlarms[*].[AlarmName,Dimensions,StateReason,StateUpdatedTimestamp,StateValue]' --output table
```

If no alarm name in alert, check what recently fired (all states):
```
aws cloudwatch describe-alarm-history --region <region> \
  --start-date <startsAt-30min ISO8601> --end-date <startsAt+2h ISO8601> \
  --history-item-type StateUpdate \
  --query 'AlarmHistoryItems[?contains(HistorySummary, `RDS`) || contains(AlarmName, `rds`) || contains(AlarmName, `db`) || contains(AlarmName, `atlas`)].[AlarmName,Timestamp,HistorySummary]' \
  --output table
```

Then list all RDS instances to confirm the exact identifier:
```
aws rds describe-db-instances --region <region> \
  --query 'DBInstances[*].[DBInstanceIdentifier,DBInstanceClass,DBInstanceStatus,MultiAZ]' --output table
```

**IMPORTANT: Always check all instances in the same cluster in parallel, not just the one in the alarm name.**
Alert names do not always map 1:1 to instance names (e.g. `atlas-customer-v1-r1` → `bap-reader-1`).
The actual high CPU may be on a different instance in the same cluster.

- If alarm mentions `customer` → check `bap-reader-1`, `bap-writer-1`, `bap-reader-3` all at once
- If alarm mentions `driver` → check `bpp-writer-1`, `bpp-reader-1` all at once

Run CPU check for all cluster instances simultaneously:
```
for instance in bap-reader-1 bap-writer-1 bap-reader-3; do
  echo "=== $instance ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
    --dimensions Name=DBInstanceIdentifier,Value=$instance \
    --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
    --period 300 --statistics Average Maximum --region <region> --output json 2>/dev/null \
    | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average | floor)%, max=\(.Maximum | floor)%"'
done
```
(Use `bpp-writer-1 bpp-reader-1` for driver alerts.)

The instance with the highest CPU is the actual affected instance — use that for all subsequent steps.

---

## Step 2: Check the Alerting Metric + Related Metrics (Run in Parallel)

For the identified instance, run all of these simultaneously:
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
  --dimensions Name=DBInstanceIdentifier,Value=<instance-id> \
  --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
  --period 300 --statistics Average Maximum --region <region>

aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections \
  --dimensions Name=DBInstanceIdentifier,Value=<instance-id> \
  --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
  --period 300 --statistics Maximum --region <region>

aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name FreeableMemory \
  --dimensions Name=DBInstanceIdentifier,Value=<instance-id> \
  --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
  --period 300 --statistics Average --region <region>

aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name WriteIOPS \
  --dimensions Name=DBInstanceIdentifier,Value=<instance-id> \
  --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
  --period 300 --statistics Average --region <region>

aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name ReadIOPS \
  --dimensions Name=DBInstanceIdentifier,Value=<instance-id> \
  --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
  --period 300 --statistics Average --region <region>
```

---

## Step 3: Find the Top Queries Using Performance Insights

This is the most important step — identifies exactly which SQL is causing high CPU:
```
aws pi describe-dimension-keys \
  --service-type RDS \
  --identifier db:<instance-id> \
  --start-time <startsAt-10min ISO8601> \
  --end-time <startsAt+1h ISO8601> \
  --metric db.load.avg \
  --group-by '{"Group":"db.sql","Limit":10}' \
  --region <region>
```

Look for: full table scans (`Seq Scan`), missing indexes, long-running transactions, N+1 patterns.
Note the top query fingerprint and which application service is likely running it.

If Performance Insights returns no data, check slow query logs:
```
aws rds describe-db-log-files --db-instance-identifier <instance-id> --region <region>
aws rds download-db-log-file-portion --db-instance-identifier <instance-id> \
  --log-file-name <most-recent-slow-query-log> --region <region>
```

---

## Step 4: Check Business Impact (Run in Parallel)

After identifying the DB issue, immediately check if it's affecting users. Run all of these simultaneously.

**Use the `prometheus_query_range` tool for all PromQL queries below. Do NOT use http_get.**

### 4a. Ride-to-Search Ratio (are riders getting matched?)
Use `prometheus_query_range` tool with:
- query: `rate(beckn_ride_created_total[5m]) / rate(beckn_search_request_total[5m])`
- start: `<startsAt - 30m>`
- end: `<startsAt + 1h>`
- step: `5m`

A drop at `startsAt` vs baseline indicates DB issues are blocking ride allocation.

### 4b. 5xx Error Rate (are APIs failing?)
Use `prometheus_query_range` tool with:
- query: `sum(rate(nginx_ingress_controller_requests{status=~"5.."}[5m])) by (ingress)`
- start: `<startsAt - 10m>`
- end: `<startsAt + 1h>`
- step: `1m`

Check if any service's 5xx rate spiked at the same time as the DB CPU spike.

### 4c. API Latency (are requests slowing down?)
Use `prometheus_query_range` tool with:
- query: `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))`
- start: `<startsAt - 10m>`
- end: `<startsAt + 1h>`
- step: `1m`

A p99 spike on `bap-app-backend` or `bpp-backend` = DB is in the critical path.

### 4d. ALB 5xx errors (fallback if prometheus_query_range returns no data)
Use bash tool:
```
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB \
  --metric-name HTTPCode_Target_5XX_Count \
  --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
  --period 300 --statistics Sum --region <region>
```

---

## Step 5: Correlate — Which Service is Hitting This DB?

Check application pods that connect to this DB:
```
timeout 30 stern -n atlas bap-app-backend --since 1h 2>/dev/null | grep -iE "db|connection|timeout|refused|deadlock|query" | head -100
timeout 30 stern -n atlas bpp-backend --since 1h 2>/dev/null | grep -iE "db|connection|timeout|refused|deadlock|query" | head -100
```

Search Elasticsearch for DB errors in `app-logs-<YYYY-MM-DD>`:
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
  "_source": ["message", "@timestamp"]
}
```

---

## Synthesis

| Pattern | Root Cause |
|---------|------------|
| High CPU + Performance Insights shows seq scan | Missing index on a hot table |
| High CPU + connection surge coincides with deploy | New code introduced expensive query or connection leak |
| High CPU + high WriteIOPS | Bulk insert/update or autovacuum running |
| High connections + DB errors in app logs | PgBouncer pool exhaustion |
| Replication lag + high WriteIOPS | Heavy write load on primary |
| DB CPU high but no 5xx / no ratio drop | Background job or analytics query — lower urgency |
| DB CPU high + 5xx spike + ratio drop | Critical — DB is blocking ride matching / user requests |

## Output Required

State clearly:
1. **Affected instance:** `<instance-id>` with `<metric>` at `<peak value>` at `<timestamp>`
2. **Top query:** `<SQL fingerprint>` — likely from service `<service-name>`
3. **Business impact:** ride-to-search ratio `<before>` → `<after>`, 5xx rate `<before>` → `<after>`
4. **Root cause:** one sentence
5. **Recommended fix:** exact action (add index on X, scale instance, fix query in service Y)
