# RDS Investigation Runbook

## Goal
- **Primary Objective:** Investigate AWS RDS alerts — high CPU, high connections, low memory, high IOPS, storage issues, or replication lag.
- **Scope:** AWS RDS (PostgreSQL) instances for SRE Platform in <region>.
- **Agent Mandate:** Read-only. Do not modify any DB settings. Provide RCA with root cause and recommended fixes for the team.
- **Expected Outcome:** Identify which instance is affected, what caused the alert, which service/query is responsible, and what the team should do.

## Time Window Instructions
- The alert's `startsAt` field contains the **exact time the alarm fired** — use this as your investigation window start.
- For all CloudWatch and Elasticsearch queries use: `start-time = startsAt - 10 minutes`, `end-time = startsAt + 1 hour`
- If `startsAt` is not available, fall back to `now - 30 minutes`.
- Always state the time window used in your findings (e.g. "investigated 17:00–18:00 UTC").

## Infrastructure Reference
- **Elasticsearch app logs index:** `app-logs-YYYY-MM-DD` (e.g. `app-logs-2026-03-12`)
- **Elasticsearch endpoint:** `https://<elasticsearch-endpoint>`
- **App log format (in message field):** `TIMESTAMP LEVEL> @pod-name [requestId-UUID, sessionId-UUID, component] |> error message`

## Workflow

### Step 1: List All RDS Instances and Find the Alerting One
First, get all RDS instances in the account:
```
aws rds describe-db-instances --region <region> --query 'DBInstances[*].[DBInstanceIdentifier,DBInstanceClass,DBInstanceStatus,MultiAZ]' --output table
```

If the alarm name contains an instance identifier, match it from the list above.
If unclear, also describe alarms to find the exact instance:
```
aws cloudwatch describe-alarms --state-value ALARM --region <region> --query 'MetricAlarms[*].[AlarmName,Dimensions,StateReason]' --output table
```

Note all instance identifiers from Step 1 — you will check CPU on ALL of them in Step 2.

### Step 2: Check CPU on ALL Instances (Find Which One is High)
For **each instance** returned in Step 1, run:
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization --dimensions Name=DBInstanceIdentifier,Value=<instance-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Average Maximum --region <region>
```

Run this for every instance. Identify which instances have high CPU (> 70% average or > 90% maximum). Focus the rest of the investigation on those.

### Step 3: Check All Key Metrics on the High-CPU Instance(s)
For each high-CPU instance found in Step 2:
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections --dimensions Name=DBInstanceIdentifier,Value=<high-cpu-instance-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Maximum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name FreeableMemory --dimensions Name=DBInstanceIdentifier,Value=<high-cpu-instance-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Average --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name ReadIOPS --dimensions Name=DBInstanceIdentifier,Value=<high-cpu-instance-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Average --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name WriteIOPS --dimensions Name=DBInstanceIdentifier,Value=<high-cpu-instance-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Average --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DiskQueueDepth --dimensions Name=DBInstanceIdentifier,Value=<high-cpu-instance-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Average --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name ReplicaLag --dimensions Name=DBInstanceIdentifier,Value=<high-cpu-instance-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Average Maximum --region <region>
```

Interpret:
- `DatabaseConnections` — connection count trend (surge = pool exhaustion or new deployment)
- `FreeableMemory` — low = buffer pool pressure
- `WriteIOPS` spike — bulk insert/update or autovacuum
- `DiskQueueDepth` > 1 sustained — disk saturated
- `ReplicaLag` — replication falling behind primary

### Step 4: Check Performance Insights for Top Queries
For the high-CPU instance (use the writer instance if it's a cluster):
```
aws pi describe-dimension-keys --service-type RDS --identifier db:<high-cpu-instance-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --metric db.load.avg --group-by '{"Group":"db.sql","Limit":10}' --region <region>
```

Look for: full table scans, missing indexes, long-running transactions, N+1 query patterns.

### Step 5: Check RDS Slow Query Logs
```
aws rds describe-db-log-files --db-instance-identifier <high-cpu-instance-id> --region <region>
aws rds download-db-log-file-portion --db-instance-identifier <high-cpu-instance-id> --log-file-name <most-recent-log-file> --region <region>
```

Look for: queries exceeding `long_query_time`, lock wait timeouts, connection errors.

### Step 6: Correlate with Application Pods
Find all pods connecting to RDS across all namespaces:
`kubectl get pods -A | grep -iE "beckn|atlas|drainer|producer|backend"`

Grep logs for DB errors from the high-CPU instance's service window:
`timeout 30 stern -n atlas bap-app-backend --since 1h | grep -iE "db|connection|timeout|refused|too many|deadlock|query" | head -200`
`timeout 30 stern -n atlas bpp-backend --since 1h | grep -iE "db|connection|timeout|refused|too many|deadlock|query" | head -200`

### Step 7: Search Elasticsearch for DB Errors
Search `app-logs-<today's date>` for DB errors in the last hour:
```json
{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "now-1h"}}},
        {"bool": {
          "should": [
            {"match": {"message": "connection refused"}},
            {"match": {"message": "too many connections"}},
            {"match": {"message": "deadlock"}},
            {"match": {"message": "query timeout"}},
            {"match": {"message": "ERROR"}}
          ],
          "minimum_should_match": 1
        }}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```
Look inside `message` field (JSON) → `log` key for the actual error. Match error timestamps to the CPU spike window from Step 2.

### Step 8: Check for Batch Jobs or Migrations
`kubectl get jobs -A --sort-by='.metadata.creationTimestamp' | tail -20`
`kubectl get cronjobs -A`

Look for data pipeline jobs, report generation, or schema migrations running during the spike.

## Synthesize Findings

- **CPU spike + top query doing seq scan** → Missing index. Report: table name, query, which service runs it.
- **CPU spike + connection surge + deployment** → New code introduced expensive query or connection leak. Report: service, deployment time.
- **CPU spike + high WriteIOPS** → Bulk insert/update or autovacuum. Report: which job or service is writing.
- **High connections + DB errors in app logs** → Connection pool exhaustion. Report: which service, connection count.
- **Replication lag + WriteIOPS spike** → Heavy write load on primary. Report: write source.
- **Low FreeableMemory + high ReadIOPS** → Buffer pool pressure, data not fitting in memory.

## Possible Fixes (for team to action)
- Add index on the column identified from slow query / seq scan
- Optimize the top CPU-consuming query
- Review PgBouncer connection pool sizing for the connecting service
- Scale up instance class if workload has genuinely grown
- Tune autovacuum settings if autovacuum is the cause
