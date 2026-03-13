# System Alerts Investigation Runbook

## Goal
- **Primary Objective:** Investigate system alerts — drainer lag, login success rate drops, producer failures, system config parse failures, and service-level 5xx errors.
- **Scope:** Backend services on EKS. Services: BAP app backend, BPP driver backend, drainer pods, job producer.
- **Expected Outcome:** Identify which service failed, why it failed, and what the likely root cause is.

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
- Redis cluster IDs
- Elasticsearch endpoint + app log index name

## Alert Types and Investigation Steps

---

### Alert: LoginSuccessRate
**Trigger:** Auth verify success rate dropped more than 90%.

1. Check current auth endpoint error rate in VictoriaMetrics:
   `rate(http_request_duration_seconds_count{status_code=~"2..", handler="/v2/auth/:authId/verify/"}[5m])` vs
   `rate(http_request_duration_seconds_count{handler="/v2/auth/:authId/verify/"}[5m])`
   to get the success ratio.

2. Find the actual auth service pods across all namespaces:
   `kubectl get pods -A | grep -i "app-backend\|auth"`
   Note the namespace, pod names, STATUS, and RESTARTS.

3. Grep logs across all auth service pods using stern (with head limit to avoid hanging):
   `timeout 30 stern -n <namespace> <app-backend-service> --since 30m | grep -iE "auth|verify|error|exception|redis|db|timeout|refused" | head -200`

5. Search the app log index (from knowledge base) for errors on the auth endpoint in the last 30 minutes:
```json
{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "verify"}},
        {"match": {"message": "ERROR"}},
        {"range": {"@timestamp": {"gte": "now-30m"}}}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```

6. Check if it's a Redis issue (auth sessions/OTPs cached in Redis):
   Look in logs for "redis", "timeout", "connection refused", "CLUSTERDOWN".
   If found → immediately run Redis pivot steps (see Synthesize section below).

7. Check if it's a DB issue:
   Look in logs for "DB", "connection refused", "too many connections", "query timeout".
   If found → immediately run RDS pivot steps (see Synthesize section below).

8. Check recent deployments:
   `kubectl get events -n <namespace> --sort-by='.lastTimestamp' | grep -iE "pulled|deploy|image" | tail -10`

**Possible root causes:** DB connectivity issue, Redis down, OTP provider outage, bad deployment, pod crash loop.

---

### Alert: ProducerNotProducing
**Trigger:** Job producer service not producing jobs for 10 minutes.

1. Find the actual producer pod name:
   `kubectl get pods -A | grep -i producer`
   Note namespace, pod name, STATUS, RESTARTS.

2. Grep producer logs for errors:
   `timeout 30 stern -n <namespace> <producer-name-from-grep> --since 30m | grep -iE "error|exception|kafka|connect|timeout|panic|stop|fail" | head -200`

4. Check Prometheus metric to confirm silence:
   `sum(increase(producer_operation_duration_sum{operation="producer"}[10m]))`

5. Check pod events:
   `kubectl describe pod -n <namespace> <pod-name> | tail -20`

**Possible root causes:** Pod crash loop, Kafka/messaging connectivity issue, OOM, bad config after deployment.

---

### Alert: NoDriverDrainerRunning / NoAppDrainerRunning
**Trigger:** Drainer stop status metric > 0 (drainer has stopped processing).

**Step 1 — Confirm metric and pod state (run in parallel):**
```
kubectl get pods -n atlas | grep -i drainer
```
```
prometheus: driver_drainer_stop_status or drainer_stop_status
```

**Step 2 — Determine which scenario applies and follow it:**

#### Scenario A: Pods are DOWN (0 replicas)
- `kubectl get deployment -n atlas | grep drainer` → AVAILABLE=0
- `kubectl get events -n atlas --sort-by=.lastTimestamp | grep -i drainer | tail -20`
- `kubectl describe pod -n atlas <last-drainer-pod> | grep -A5 "Last State\|OOMKilled\|Reason"`
- **Root cause:** Node eviction, OOM kill, or manual scale-down
- **Fix:** `kubectl scale deployment/<drainer> -n atlas --replicas=5`

#### Scenario B: Pods RUNNING + stop_status=1 → Query execution error (most likely)
The drainer processes a queue of DB writes. A single bad record with a fatal SQL error causes the drainer to halt internally while the pod stays alive.
- `kubectl logs -n atlas -l app=<drainer-label> --since=30m | grep -iE "sqlState|BATCH_INSERT|integer out of range|constraint|deadlock|error|panic" | head -100`
- Look for: `sqlState 22003` (integer overflow), `BATCH_INSERT_FAILED`, `constraint violation`
- **Root cause:** Bad/corrupt record in drainer queue causing repeated SQL failures → drainer halts
- **Fix:** `kubectl rollout restart deployment/<drainer> -n atlas` to clear stopped state, then fix the bad record or schema

#### Scenario C: Pods RUNNING + stop_status=1 → DB connectivity issue
- `kubectl logs -n atlas -l app=<drainer-label> --since=30m | grep -iE "connection refused|too many connections|timeout|postgres" | head -100`
- Check RDS CPU + connections for driver RDS instances (atlas-driver-w3, driver-r1) via CloudWatch
- **Root cause:** DB overloaded, connection pool exhausted, or RDS failover in progress
- **Fix:** Resolve DB issue first, then restart drainer

#### Scenario D: Pods RUNNING, no SQL/DB errors → Metrics anomaly
- Verify: `prometheus: rate(driver_query_drain_latency_count[5m])` — if > 0, drainer IS processing (false alert)
- **Root cause:** Stale metric or transient spike
- **Fix:** Monitor for 5 min — if processing rate is non-zero, alert self-resolved

**Likelihood order:** Query execution error → DB connectivity → Pod eviction/OOM → Metrics anomaly

---

### Alert: NoDriverDrainerPodRunning / NoCustomerDrainerPodRunning
**Trigger:** Zero available replicas for drainer deployment.

1. Find drainer deployments:
   `kubectl get deployments -A | grep -i drainer`

2. Check events for why pods aren't starting:
   `kubectl get events -A --sort-by='.lastTimestamp' | grep -i drainer | tail -20`

3. Check node capacity:
   `kubectl describe nodes | grep -A5 "Allocated resources"`

**Possible root causes:** Image pull failure, node out of resources, persistent crash loop.

---

### Alert: DriverDrainerLagIncreasing / CustomerDrainerLagIncreasing
**Trigger:** Drainer processing lag exceeds threshold.

1. Confirm lag in VictoriaMetrics:
   - Driver: `(sum(increase(driver_query_drain_latency_sum[5m])) / sum(increase(driver_query_drain_latency_count[5m]))) / (1000*60*60)`
   - Customer: `(sum(increase(query_drain_latency_sum[5m])) / sum(increase(query_drain_latency_count[5m]))) / (1000*60*60)`

2. Find drainer pods and check their health:
   `kubectl get pods -A | grep -i drainer`

3. Grep drainer logs for slow processing or DB errors:
   `timeout 30 stern -n <namespace> <drainer-name> --since 30m | grep -iE "slow|lag|db|timeout|error|exception" | head -200`

4. Check RDS connections and CPU — use the RDS instance IDs from the knowledge base (lag usually means DB is the bottleneck):
   ```
   for instance in <writer-instance-1> <writer-instance-2>; do
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

5. Check if drainer pod count dropped (HPA scale-down):
   `kubectl get hpa -n <namespace> | grep -i drainer`

**Possible root causes:** DB overloaded, drainer pods reduced, traffic spike, slow query from recent deployment.

---

### Alert: SystemConfigParseFailure
**Trigger:** `system_configs_failed_counter` > 15 events in 1 minute.

1. Find which service is emitting the failure:
   Query VictoriaMetrics: `sum(increase(system_configs_failed_counter[1m])) by (service, event)`

2. Find that service's pods:
   `kubectl get pods -A | grep -i <service-name-from-step-1>`

3. Grep logs for config errors:
   `timeout 30 stern -n <namespace> <service-name> --since 15m | grep -iE "config|parse|failed|error|invalid|syntax" | head -200`

5. Check recent ConfigMap/Secret changes:
   `kubectl get events -A --sort-by='.lastTimestamp' | grep -iE "configmap|secret|updated" | tail -10`

**Possible root causes:** Bad config in DB/config service, malformed YAML/JSON, wrong env var after deployment.

---

### Alert: TrinetraTriggered
**Trigger:** A Trinetra-monitored job failed (job_status_total with non-OK status > 0).

1. Find which job failed and what status:
   `sum(increase(job_status_total{status!="OK", job_name!~"Custom cpu .*"}[5m])) by (job_name, status)`

2. Find the pod running that job:
   `kubectl get pods -A | grep -i <job-name-from-step-1>`

3. Grep pod logs for failure reason:
   `timeout 30 stern -n <namespace> <pod-name> --since 30m | grep -iE "error|exception|fail|panic|timeout" | head -200`

5. Check job history:
   `kubectl get jobs -A | grep -i <job-name>`

**Possible root causes:** External dependency failure, DB connection error, data inconsistency, timeout from downstream.

---

### Alert: Multimodal5xx
**Trigger:** Multimodal API returning > 10 5xx errors in 1 minute.

1. Find which service and handler is failing:
   `sum(increase(http_request_duration_seconds_count{handler=~".*multimodal.*", status_code=~"5[0-9]{2}"}[5m])) by (service, handler, status_code)`

2. Find that service's pods:
   `kubectl get pods -A | grep -i <service-name-from-step-1>`

3. Grep logs for multimodal errors:
   `timeout 30 stern -n <namespace> <service-name> --since 30m | grep -iE "multimodal|error|exception|timeout|5[0-9]{2}" | head -200`

4. Search the app log index (from knowledge base) for 5xx responses on multimodal handlers in the last 30 minutes:
```json
{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"match": {"message": "multimodal"}},
        {"range": {"@timestamp": {"gte": "now-30m"}}}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```

6. If logs show Redis errors → run Redis pivot (see Synthesize section).
   If logs show DB errors → run RDS pivot (see Synthesize section).

**Possible root causes:** External multimodal API outage, timeout from provider, bad request format, rate limit.

---

## General Steps (run for every alert)

1. Check overall pod health: `kubectl get pods -A | grep -vE "Running|Completed"`
2. Check nodes: `kubectl get nodes`
3. Check recent deployments: `kubectl get events -A --sort-by='.lastTimestamp' | grep -iE "pulled|deploy|image" | tail -20`

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
RDS is the root cause. Use the service→RDS mapping from the knowledge base. Immediately run for all relevant instances:
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
Report: RDS CPU%, connection count, top queries.

### If single pod crash loop → application bug or OOM. Report pod name, restart count, last error from logs.
### If all pods of a service down → image pull failure or resource quota. Report deployment events.
### If lag + DB slow → DB is bottleneck. Report RDS CPU and connection count.
### If config parse failure after deployment → bad config in new release. Report which service and config field.

---

## Extended Investigation (if runbook steps did not find root cause)

If you have followed all the steps above and still cannot determine the root cause with HIGH or MEDIUM confidence, do not stop. Use your own judgment to continue investigating using any tools available. Consider:
- Correlate timestamps across all signals — metrics spike, log errors, pod restarts, deployments
- Check services that this component depends on (upstream/downstream)
- Look for patterns: is this affecting one pod or all? One namespace or cluster-wide?
- Check recent changes: deployments, config changes, scaling events in the last 2 hours
- Query Elasticsearch for error patterns around the incident time
- Check Prometheus for any other anomalous metrics correlated with the alert time
- Use kubectl to inspect pod resource usage, node pressure, or scheduling issues

The goal is to find the root cause — the runbook covers the most likely scenarios but real incidents can be unexpected. Trust your investigation instincts and follow the evidence.

