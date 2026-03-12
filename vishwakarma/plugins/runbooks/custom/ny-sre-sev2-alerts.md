# SRE Platform SRE SEV2 Alerts Investigation Runbook

## Goal
- **Primary Objective:** Investigate SEV2-level alerts — Beckn 5xx errors, external gateway failures (Juspay, ONDC), ride-to-search ratio drops, and allocator issues.
- **Scope:** SRE Platform backend on EKS `atlas` namespace plus external dependencies.
- **Expected Outcome:** Identify the failing service, whether it is internal or external, and the likely cause.

## Time Window Instructions
- The alert's `startsAt` field contains the **exact time the alarm fired** — use this as your investigation window start.
- For all CloudWatch, VictoriaMetrics, and Elasticsearch queries use: `start-time = startsAt - 10 minutes`, `end-time = startsAt + 1 hour`
- For stern/kubectl logs use `--since` calculated from startsAt (e.g. if startsAt was 30 min ago, use `--since 40m`).
- If `startsAt` is not available, fall back to `now - 30 minutes`.
- Always state the time window used in your findings.

## Infrastructure Reference
- **RDS instances:**
  - Customer/BAP: `bap-reader-1` (read), `bap-writer-1` (write), `bap-reader-3` (read)
  - Driver/BPP: `bpp-writer-1` (write), `bpp-reader-1` (read)
- **Redis clusters:** `main-redis-cluster` (main), `location-redis` (location tracking), `utils-redis-cluster`
- **Istio access log index:** `istio-YYYY.MM.DD` — has HTTP status codes, request IDs, upstream services
  - Log format: `[timestamp] "METHOD /path HTTP/1.1" STATUS_CODE ... "request-id-uuid" "host" "upstream-ip" outbound|port|version|service.namespace.svc.cluster.local`
- **App log index:** `app-logs-YYYY-MM-DD` — `message` field is JSON with `log` key containing the actual error text
- **Elasticsearch endpoint:** `https://<elasticsearch-endpoint>`

---

### Alert: Beckn5xxErrors / BecknExternalAPI5xx / BecknIncomingAPI5xx
**Trigger:** Beckn API returning 5xx errors above threshold.

1. Find which service and API handler is failing — query VictoriaMetrics:
   `sum(increase(http_request_duration_seconds_count{status_code=~"5[0-9]{2}"}[5m])) by (service, handler, status_code)`
   Note the top service name and handler.

2. Find that service's pods across all namespaces:
   `kubectl get pods -A | grep -i <service-name-from-step-1>`
   Note namespace, pod names, STATUS, RESTARTS.

3. Grep logs across all pods of that service:
   `timeout 30 stern -n atlas <service-name> --since 30m | grep -iE "error|exception|panic|5[0-9]{2}|db|redis|timeout|refused" | head -200`

4. Search Elasticsearch `istio-<today's date>` for 5xx HTTP responses in the last 30 minutes:
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
From results extract: which API path, which STATUS_CODE, which upstream service (from `outbound|...|service.namespace`), and 2-3 request ID UUIDs.

5. Use request IDs from step 4 to find full error in `app-logs-<today's date>`:
```json
{
  "size": 10,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "<request-id-uuid-from-step-4>"}},
        {"range": {"@timestamp": {"gte": "now-30m"}}}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```
Look in `message` field (JSON) → `log` key for exception type, Redis timeout, DB error, null pointer.

6. Check if an external provider caused the 5xx:
   `timeout 30 stern -n atlas <service-name> --since 30m | grep -iE "juspay|ondc|google|external|downstream|outbound|provider" | head -50`

7. Check pod OOM/crash events:
   `kubectl describe pod -n atlas <pod-name> | grep -A5 "Last State\|OOMKilled\|Reason"`

8. Check recent deployments:
   `kubectl get events -A --sort-by='.lastTimestamp' | grep -i <service-name> | tail -10`

**Then go to Synthesize section and act on what the logs show.**

---

### Alert: JuspayGateway5xx / JuspayRegistry5xx
**Trigger:** Juspay gateway or registry returning 5xx errors.

1. Find which service is making Juspay calls and getting 5xx:
   `sum(increase(http_request_duration_seconds_count{status_code=~"5[0-9]{2}"}[5m])) by (service, handler)`
   Look for handlers with "juspay", "payment", "gateway" in the name.

2. Find that service's pods: `kubectl get pods -A | grep -i <service-name>`

3. Grep logs for Juspay errors:
   `timeout 30 stern -n atlas <service-name> --since 30m | grep -iE "juspay|payment|gateway|5[0-9]{2}|error|timeout" | head -200`

4. Search Elasticsearch `app-logs-<today's date>` for Juspay-related errors in the last 30 minutes:
```json
{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "juspay"}},
        {"range": {"@timestamp": {"gte": "now-30m"}}}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```

6. Determine: is Juspay returning 5xx (external outage) or is SRE Platform sending bad requests (request format issue)?
   Look for HTTP response body in logs — a 5xx with Juspay's error message = external issue.

**Possible root causes:** Juspay outage, expired API credentials, bad request format after code change.

---

### Alert: ONDCGateway5xx / ONDCRegistry5xx
**Trigger:** ONDC gateway or registry returning 5xx errors.

1. Find which service is making ONDC calls:
   `sum(increase(http_request_duration_seconds_count{status_code=~"5[0-9]{2}"}[5m])) by (service, handler)`

2. Find that service's pods: `kubectl get pods -A | grep -i <service-name>`

3. Grep logs for ONDC errors:
   `timeout 30 stern -n atlas <service-name> --since 30m | grep -iE "ondc|registry|gateway|5[0-9]{2}|error|timeout" | head -200`

**Possible root causes:** ONDC outage, API version mismatch, bad credentials, network issue.

---

### Alert: RideToSearchRatioDown
**Trigger:** Ratio of ride bookings to searches dropped significantly.

1. Check current ratio in VictoriaMetrics — look for metrics with "ride", "search", "booking" in name:
   `sum(rate({__name__=~".*ride.*total.*"}[5m]))` and `sum(rate({__name__=~".*search.*total.*"}[5m]))`

2. Find search and booking service pods:
   `kubectl get pods -A | grep -iE "search|booking|alloc"`

3. Grep logs for errors on search and booking:
   `timeout 30 stern -n atlas <service-name> --since 30m | grep -iE "error|exception|search|book|match|driver|timeout" | head -200`

4. Check drainer health (stale driver data = poor match rate):
   `kubectl get pods -A | grep -i drainer`
   Check drainer lag metric: `driver_drainer_stop_status`

5. Check Redis (driver location data is in `location-redis` — evictions = stale locations):
   ```
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name Evictions --dimensions Name=ReplicationGroupId,Value=location-redis --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Sum --region <region>
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization --dimensions Name=ReplicationGroupId,Value=location-redis --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average Maximum --region <region>
   ```

**Possible root causes:** Drainer lag (stale driver locations), Redis evictions on `location-redis`, search/booking service error, DB overload.

---

### Alert: AllocatorLooksDead
**Trigger:** Allocator service is not functioning.

1. Find allocator pods: `kubectl get pods -A | grep -iE "alloc"`
   Note STATUS and RESTARTS.

2. Grep allocator logs:
   `timeout 30 stern -n atlas <allocator-name-from-grep> --since 30m | grep -iE "error|exception|redis|db|timeout|refused|panic|dead" | head -200`

4. Check Redis (allocator depends heavily on driver location data in `location-redis`):
   ```
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization --dimensions Name=ReplicationGroupId,Value=location-redis --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average Maximum --region <region>
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name DatabaseMemoryUsagePercentage --dimensions Name=ReplicationGroupId,Value=location-redis --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average --region <region>
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name Evictions --dimensions Name=ReplicationGroupId,Value=location-redis --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Sum --region <region>
   ```

5. Check pod OOM/crash: `kubectl describe pod -n atlas <pod-name> | grep -A5 "Last State\|OOMKilled"`

**Possible root causes:** Allocator crashed/OOM, Redis (`location-redis`) unavailable, DB overload, bad deployment.

---

## General Steps (run for every alert)

1. `kubectl get pods -A | grep -vE "Running|Completed"` — any failing pods?
2. `kubectl get nodes` — any NotReady nodes?
3. `kubectl get events -A --sort-by='.lastTimestamp' | grep -iE "pulled|deploy|image|oom" | tail -20`

---

## Synthesize Findings

### If logs show Redis errors (timeout, connection refused, CLUSTERDOWN, MOVED)
Redis is the root cause. Immediately run for all clusters:
```
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization --dimensions Name=ReplicationGroupId,Value=main-redis-cluster --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average Maximum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name DatabaseMemoryUsagePercentage --dimensions Name=ReplicationGroupId,Value=main-redis-cluster --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name Evictions --dimensions Name=ReplicationGroupId,Value=main-redis-cluster --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Sum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name CurrConnections --dimensions Name=ReplicationGroupId,Value=main-redis-cluster --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Maximum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization --dimensions Name=ReplicationGroupId,Value=location-redis --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average Maximum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name DatabaseMemoryUsagePercentage --dimensions Name=ReplicationGroupId,Value=location-redis --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name Evictions --dimensions Name=ReplicationGroupId,Value=location-redis --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Sum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name CurrConnections --dimensions Name=ReplicationGroupId,Value=location-redis --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Maximum --region <region>
```
Report: cluster name, CPU%, memory%, eviction count, connection count, and when it correlated with the alert.

### If logs show DB errors (connection refused, too many connections, query timeout, deadlock)
RDS is the root cause. Immediately run for Customer (BAP):
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization --dimensions Name=DBInstanceIdentifier,Value=bap-reader-1 --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average Maximum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections --dimensions Name=DBInstanceIdentifier,Value=bap-reader-1 --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Maximum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization --dimensions Name=DBInstanceIdentifier,Value=bap-writer-1 --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average Maximum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections --dimensions Name=DBInstanceIdentifier,Value=bap-writer-1 --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Maximum --region <region>
aws pi describe-dimension-keys --service-type RDS --identifier db:bap-writer-1 --start-time <30min ago ISO8601> --end-time <now ISO8601> --metric db.load.avg --group-by '{"Group":"db.sql","Limit":5}' --region <region>
```
For Driver (BPP):
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization --dimensions Name=DBInstanceIdentifier,Value=bpp-writer-1 --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Average Maximum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections --dimensions Name=DBInstanceIdentifier,Value=bpp-writer-1 --start-time <30min ago ISO8601> --end-time <now ISO8601> --period 60 --statistics Maximum --region <region>
aws pi describe-dimension-keys --service-type RDS --identifier db:bpp-writer-1 --start-time <30min ago ISO8601> --end-time <now ISO8601> --metric db.load.avg --group-by '{"Group":"db.sql","Limit":5}' --region <region>
```
Report: instance name, CPU%, connection count, top queries from Performance Insights.

### If logs show external provider errors (juspay, ondc, timeout to external URL)
External dependency is the root cause. Report: which provider, error message, whether transient or sustained.

### If OOMKilled pods
Pod memory limit exceeded. Report: pod name, memory limit, restart count.

### If all services 5xx + recent deployment
Bad deployment. Report: service name, image tag, deploy time vs alert start time.

### If specific API only + code exception
Application bug in that API. Report: handler name, exception type, first occurrence time.
