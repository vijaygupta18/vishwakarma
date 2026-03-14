# SRE SEV2 Alerts Investigation Runbook

## Goal
- **Primary Objective:** Investigate SEV2-level alerts — service 5xx errors, external gateway failures, ride-to-search ratio drops, and allocator issues.
- **Scope:** Backend services on EKS plus external dependencies.
- **Expected Outcome:** Identify the failing service, whether it is internal or external, and the likely cause.

## Time Window Instructions
- The alert's `startsAt` field contains the **exact time the alarm fired** — use this as your investigation window start.
- For all CloudWatch, VictoriaMetrics, and Elasticsearch queries use: `start-time = startsAt - 10 minutes`, `end-time = startsAt + 1 hour`
- For stern/kubectl logs use `--since` calculated from startsAt (e.g. if startsAt was 30 min ago, use `--since 40m`).
- If `startsAt` is not available, fall back to `now - 30 minutes`.
- Always state the time window used in your findings.

## Infrastructure Reference
Refer to the **Site Knowledge Base** for your cluster's specific values:
- Service namespaces and pod name patterns
- RDS instance identifiers (BAP/BPP, reader/writer)
- Redis cluster IDs and their roles
- Istio access log index name + app log index name
- Elasticsearch endpoint

---

### Alert: Beckn5xxErrors / ExternalAPI5xx / IncomingAPI5xx
**Trigger:** API returning 5xx errors above threshold.

1. Find which service and API handler is failing — query VictoriaMetrics:
   `sum(increase(http_request_duration_seconds_count{status_code=~"5[0-9]{2}"}[5m])) by (service, handler, status_code)`
   Note the top service name and handler.

2. Find that service's pods across all namespaces:
   `kubectl get pods -A | grep -i <service-name-from-step-1>`
   Note namespace, pod names, STATUS, RESTARTS.

3. Grep logs across all pods of that service:
   `timeout 30 stern -n <namespace> <service-name> --since 30m | grep -iE "error|exception|panic|5[0-9]{2}|db|redis|timeout|refused" | head -200`

4. Search the Istio access log index (from knowledge base) for 5xx HTTP responses in the last 30 minutes:
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

5. Use request IDs from step 4 to find full error in the app log index (from knowledge base):
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
   `timeout 30 stern -n <namespace> <service-name> --since 30m | grep -iE "external|downstream|outbound|provider|payment|gateway" | head -50`

7. Check pod OOM/crash events:
   `kubectl describe pod -n <namespace> <pod-name> | grep -A5 "Last State\|OOMKilled\|Reason"`

8. Check recent deployments:
   `kubectl get events -A --sort-by='.lastTimestamp' | grep -i <service-name> | tail -10`

**Then go to Synthesize section and act on what the logs show.**

---

### Alert: PaymentGateway5xx / PaymentRegistry5xx
**Trigger:** Payment gateway or registry returning 5xx errors.

1. Find which service is making payment gateway calls and getting 5xx:
   `sum(increase(http_request_duration_seconds_count{status_code=~"5[0-9]{2}"}[5m])) by (service, handler)`
   Look for handlers with "payment", "gateway" in the name.

2. Find that service's pods: `kubectl get pods -A | grep -i <service-name>`

3. Grep logs for payment gateway errors:
   `timeout 30 stern -n <namespace> <service-name> --since 30m | grep -iE "payment|gateway|5[0-9]{2}|error|timeout" | head -200`

4. Search the app log index (from knowledge base) for payment-related errors in the last 30 minutes:
```json
{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "payment"}},
        {"range": {"@timestamp": {"gte": "now-30m"}}}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```

6. Determine: is the payment gateway returning 5xx (external outage) or are we sending bad requests (request format issue)?
   Look for HTTP response body in logs — a 5xx with the gateway's error message = external issue.

**Possible root causes:** Payment gateway outage, expired API credentials, bad request format after code change.

---

### Alert: ExternalGateway5xx / ExternalRegistry5xx
**Trigger:** External gateway or registry returning 5xx errors.

1. Find which service is making external calls:
   `sum(increase(http_request_duration_seconds_count{status_code=~"5[0-9]{2}"}[5m])) by (service, handler)`

2. Find that service's pods: `kubectl get pods -A | grep -i <service-name>`

3. Grep logs for external gateway errors:
   `timeout 30 stern -n <namespace> <service-name> --since 30m | grep -iE "registry|gateway|5[0-9]{2}|error|timeout" | head -200`

4. Search the app log index (from knowledge base) for external gateway errors in the last 30 minutes:
```json
{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "now-30m"}}},
        {"bool": {
          "should": [
            {"match": {"message": "registry"}},
            {"match": {"message": "gateway"}},
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

**Possible root causes:** External provider outage, API version mismatch, bad credentials, network issue.

---

### Alert: RideToSearchRatioDown
**Trigger:** Ratio of ride bookings to searches dropped significantly.

1. Check current ratio in VictoriaMetrics — use ride-created and search-request counter metrics from the knowledge base:
   `sum(rate({__name__=~".*ride.*total.*"}[5m]))` and `sum(rate({__name__=~".*search.*total.*"}[5m]))`

2. Find search and booking service pods:
   `kubectl get pods -A | grep -iE "search|booking|alloc"`

3. Grep logs for errors on search and booking:
   `timeout 30 stern -n <namespace> <service-name> --since 30m | grep -iE "error|exception|search|book|match|driver|timeout" | head -200`

4. Check drainer health (stale driver data = poor match rate):
   `kubectl get pods -A | grep -i drainer`
   Check drainer lag metric: `driver_drainer_stop_status`

5. Check location-tracking Redis (driver location data — evictions = stale locations). Use the location Redis cluster ID from knowledge base:
   ```
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name Evictions \
     --dimensions Name=ReplicationGroupId,Value=<location-redis-cluster-id> \
     --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
     --period 60 --statistics Sum --region <region>
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization \
     --dimensions Name=ReplicationGroupId,Value=<location-redis-cluster-id> \
     --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
     --period 60 --statistics Average Maximum --region <region>
   ```

**Possible root causes:** Drainer lag (stale driver locations), Redis evictions on location-tracking cluster, search/booking service error, DB overload.

---

### Alert: AllocatorLooksDead
**Trigger:** Allocator service is not functioning.

1. Find allocator pods: `kubectl get pods -A | grep -iE "alloc"`
   Note STATUS and RESTARTS.

2. Grep allocator logs:
   `timeout 30 stern -n <namespace> <allocator-name-from-grep> --since 30m | grep -iE "error|exception|redis|db|timeout|refused|panic|dead" | head -200`

4. Check location-tracking Redis (allocator depends heavily on driver location data). Use cluster ID from knowledge base:
   ```
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization \
     --dimensions Name=ReplicationGroupId,Value=<location-redis-cluster-id> \
     --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
     --period 60 --statistics Average Maximum --region <region>
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name DatabaseMemoryUsagePercentage \
     --dimensions Name=ReplicationGroupId,Value=<location-redis-cluster-id> \
     --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
     --period 60 --statistics Average --region <region>
   aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name Evictions \
     --dimensions Name=ReplicationGroupId,Value=<location-redis-cluster-id> \
     --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
     --period 60 --statistics Sum --region <region>
   ```

5. Check pod OOM/crash: `kubectl describe pod -n <namespace> <pod-name> | grep -A5 "Last State\|OOMKilled"`

**Possible root causes:** Allocator crashed/OOM, location-tracking Redis unavailable, DB overload, bad deployment.

---

## General Steps (run for every alert)

1. `kubectl get pods -A | grep -vE "Running|Completed"` — any failing pods?
2. `kubectl get nodes` — any NotReady nodes?
3. `kubectl get events -A --sort-by='.lastTimestamp' | grep -iE "pulled|deploy|image|oom" | tail -20`

---

## Synthesize Findings

### If logs show Redis errors (timeout, connection refused, CLUSTERDOWN, MOVED)
Redis is the root cause. Immediately run for all clusters (use cluster IDs from knowledge base):
```
for cluster in <cluster-id-1> <cluster-id-2> <cluster-id-3>; do
  echo "=== $cluster ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization \
    --dimensions Name=ReplicationGroupId,Value=$cluster \
    --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
    --period 60 --statistics Average Maximum --region <region>
  aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name DatabaseMemoryUsagePercentage \
    --dimensions Name=ReplicationGroupId,Value=$cluster \
    --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
    --period 60 --statistics Average --region <region>
  aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name Evictions \
    --dimensions Name=ReplicationGroupId,Value=$cluster \
    --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
    --period 60 --statistics Sum --region <region>
  aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name CurrConnections \
    --dimensions Name=ReplicationGroupId,Value=$cluster \
    --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
    --period 60 --statistics Maximum --region <region>
done
```
Report: cluster name, CPU%, memory%, eviction count, connection count, and when it correlated with the alert.

### If logs show DB errors (connection refused, too many connections, query timeout, deadlock)
RDS is the root cause. Use the service→RDS mapping from the knowledge base. Run for all relevant instances:
```
for instance in <bap-reader> <bap-writer> <bpp-writer> <bpp-reader>; do
  echo "=== $instance ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
    --dimensions Name=DBInstanceIdentifier,Value=$instance \
    --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
    --period 60 --statistics Average Maximum --region <region>
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name DatabaseConnections \
    --dimensions Name=DBInstanceIdentifier,Value=$instance \
    --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
    --period 60 --statistics Maximum --region <region>
done
```
For writer instances, also run Performance Insights:
```
aws pi describe-dimension-keys --service-type RDS --identifier db:<writer-instance-dbi-resource-id> \
  --start-time <startsAt-10min ISO8601> --end-time <startsAt+1h ISO8601> \
  --metric db.load.avg --group-by '{"Group":"db.sql","Limit":5}' --region <region>
```
Report: instance name, CPU%, connection count, top queries from Performance Insights.

### If logs show external provider errors (payment gateway, external registry, timeout to external URL)
External dependency is the root cause. Report: which provider, error message, whether transient or sustained.

### If OOMKilled pods
Pod memory limit exceeded. Report: pod name, memory limit, restart count.

### If all services 5xx + recent deployment
Bad deployment. Report: service name, image tag, deploy time vs alert start time.

### If specific API only + code exception
Application bug in that API. Report: handler name, exception type, first occurrence time.

---

## If You Still Don't Have the Answer

The steps above cover the most common scenarios. If root cause is still unclear — **use your own judgment**. You have full access to kubectl, Prometheus, Elasticsearch, and CloudWatch. Follow the evidence wherever it leads.

Good places to look:
- What changed recently? `kubectl get events -A --sort-by='.lastTimestamp' | tail -30`
- Is it one pod or all pods of that service?
- Is it one service or cluster-wide?
- Does the timing correlate with a deployment, traffic spike, or cron job?
- Check upstream dependencies (DB, Redis, external APIs) even if logs don't explicitly mention them
- Search ES broadly with just `ERROR` + the time window if targeted queries return nothing

