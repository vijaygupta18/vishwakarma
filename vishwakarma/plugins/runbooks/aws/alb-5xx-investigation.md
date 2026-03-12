***REMOVED*** 5xx Error Investigation Runbook

## Goal
- **Primary Objective:** When an ALB 5xx CloudWatch alarm fires, identify exactly which API endpoints are returning 5xx errors, find the root cause from logs, and report with possible fixes.
- **Scope:** AWS ALB `app/<alb-arn-suffix>` in <region>.
- **Agent Mandate:** Read-only investigation. Do not modify any infrastructure.
- **Expected Outcome:** Exact API, error message, root cause, and possible fixes.

## Time Window Instructions
- The alert's `startsAt` field contains the **exact time the alarm fired** — use this as your investigation window start.
- For all CloudWatch and Elasticsearch queries use: `start-time = startsAt - 10 minutes`, `end-time = startsAt + 1 hour`
- If `startsAt` is not available, fall back to `now - 30 minutes`.
- Always state the time window used in your findings (e.g. "investigated 17:00–18:00 UTC").

## Infrastructure Reference
- **ALB:** `app/<alb-arn-suffix>`
- **Elasticsearch:** `https://<elasticsearch-endpoint>`
- **Istio access log index:** `istio-YYYY.MM.DD` (e.g. `istio-2026.03.12`) — has HTTP status codes, request IDs, upstream services
- **Application log index:** `app-logs-YYYY-MM-DD` (e.g. `app-logs-2026-03-12`) — has full ERROR logs with requestId
- **Istio log format:** `[timestamp] "METHOD /path HTTP/1.1" STATUS_CODE ... "request-id-uuid" "host" "upstream" outbound|port|version|service.namespace.svc.cluster.local`
- **App log format (in message field):** `TIMESTAMP LEVEL> @pod-name [requestId-UUID, sessionId-UUID, component] |> error message`
- **RDS instances:** `bap-reader-1`, `bap-writer-1`, `bap-reader-3` (customer/BAP), `bpp-writer-1`, `bpp-reader-1` (driver/BPP)
- **Redis clusters:** `main-redis-cluster` (main), `location-redis` (location tracking), `utils-redis-cluster`

## Workflow

### Step 1: Confirm ALB 5xx and Get Count
```
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name HTTPCode_Target_5XX_Count --dimensions Name=LoadBalancer,Value=app/<alb-arn-suffix> --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Sum --region <region>
```
Also check ELB-generated 5xx (502/503/504 from ALB itself):
```
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name HTTPCode_ELB_5XX_Count --dimensions Name=LoadBalancer,Value=app/<alb-arn-suffix> --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Sum --region <region>
```

### Step 2: Check Response Latency (Timeout vs Crash)
```
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB --metric-name TargetResponseTime --dimensions Name=LoadBalancer,Value=app/<alb-arn-suffix> --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average p99 --region <region>
```
- High latency (> 5s) + 5xx = timeout (504). Instant 5xx = application crash (500/502).

### Step 3: Find Failing APIs from Istio Access Logs
Search `istio-<today's date>` index for 5xx HTTP responses in the last 30 minutes.

Use the Elasticsearch tool to search:
- Index: `istio-YYYY.MM.DD` (use today's date)
- Query: search `log` field for the pattern `HTTP/1.1" 5` to find 5xx responses
- The log line format: `[timestamp] "METHOD /path HTTP/1.1" STATUS_CODE ...`
- Extract: which `/path` (API endpoint), which STATUS_CODE (500/502/503/504), which `outbound|...|service.namespace` (upstream service)
- Also extract the **request ID UUID** (the quoted UUID before the host field)

Example search query:
```json
{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"match": {"log": "HTTP"}},
        {"range": {"@timestamp": {"gte": "now-30m"}}}
      ],
      "should": [
        {"match": {"log": "\" 500 "}},
        {"match": {"log": "\" 502 "}},
        {"match": {"log": "\" 503 "}},
        {"match": {"log": "\" 504 "}}
      ],
      "minimum_should_match": 1
    }
  },
  "_source": ["log", "@timestamp"]
}
```

From results, identify:
- Which API paths are returning 5xx
- Which upstream service (from `outbound|...|service.namespace.svc.cluster.local`)
- Note 2-3 request IDs from the 5xx entries

### Step 4: Get Full Error Details from Application Logs
Use the request IDs found in Step 3 to find the full error in `app-logs-YYYY-MM-DD`.

Search for each request ID:
```json
{
  "size": 10,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "<request-id-from-step-3>"}},
        {"range": {"@timestamp": {"gte": "now-30m"}}}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```

Also search for ERROR-level logs from the identified service:
```json
{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "ERROR"}},
        {"match": {"message": "<service-name-from-step-3>"}},
        {"range": {"@timestamp": {"gte": "now-30m"}}}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```

The `message` field contains JSON — look inside for `log` key which has the actual error message.
Look for: exception type, Redis timeout, DB connection refused, null pointer, OOM.

### Step 5: Correlate with Kubernetes Pod Health
Find pods for the failing service:
`kubectl get pods -A | grep -i <service-name-from-step-3>`

Check for crash loops or OOMKilled:
`kubectl describe pod -n atlas <pod-name> | grep -A5 "Last State\|OOMKilled\|Reason"`

Grep live pod logs for the same error pattern:
`timeout 30 stern -n atlas <service-name> --since 30m | grep -iE "error|exception|redis|db|timeout|refused" | head -200`

Check recent deployments:
`kubectl get events -A --sort-by='.lastTimestamp' | grep -iE "pulled|deploy|image" | tail -10`

### Step 6: Check Dependencies Based on Error Found
**→ Go to Synthesize section and act on what the error message shows.**

## Synthesize Findings

### If error message shows Redis errors (timeout, connection refused, CLUSTERDOWN, MOVED)
Redis is the root cause. Immediately run for each cluster (`main-redis-cluster`, `location-redis`):
```
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization --dimensions Name=ReplicationGroupId,Value=main-redis-cluster --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average Maximum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name DatabaseMemoryUsagePercentage --dimensions Name=ReplicationGroupId,Value=main-redis-cluster --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name Evictions --dimensions Name=ReplicationGroupId,Value=main-redis-cluster --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Sum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name CurrConnections --dimensions Name=ReplicationGroupId,Value=main-redis-cluster --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Maximum --region <region>
```
Report: CPU%, memory%, evictions, connections for `main-redis-cluster` and `location-redis`.

### If error message shows DB errors (connection refused, too many connections, query timeout, deadlock)
RDS is the root cause. Check the relevant instance based on which service failed:
- bap-app-backend (customer/BAP) → check `bap-reader-1`, `bap-writer-1`
- bpp-backend (driver/BPP) → check `bpp-writer-1`, `bpp-reader-1`

```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization --dimensions Name=DBInstanceIdentifier,Value=bap-reader-1 --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average Maximum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections --dimensions Name=DBInstanceIdentifier,Value=bap-reader-1 --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Maximum --region <region>
aws pi describe-dimension-keys --service-type RDS --identifier db:bap-reader-1 --start-time <30min ago ISO8601> --end-time <now ISO8601> --metric db.load.avg --group-by '{"Group":"db.sql","Limit":5}' --region <region>
```

### If high TargetResponseTime (> 5s) + 504 → downstream timeout. Report which dependency is slow.
### If unhealthy targets + OOMKilled pods → memory limit exceeded. Report pod name and memory limit.
### If specific API only + code exception → application bug. Report handler, exception, first occurrence time, recent deployment.
### If all APIs 5xx + recent deployment → bad deployment. Report service, image tag, deploy time.
### If ELB 5xx (not Target 5xx) → ALB cannot reach pods. Check if all pods are down.
