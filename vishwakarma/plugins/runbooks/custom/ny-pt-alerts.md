# SRE Platform Public Transit (PT) Alerts Runbook

## Goal
- **Scope:** Public Transit integration alerts — external PT APIs (CMRL, CRIS), GTFS In-Memory Server (GIMS), GRPC notifications, and business metric alerts (refunds).
- **Agent Mandate:** Read-only. Identify whether the issue is on our side or external provider side, find root cause, recommend action.
- **Expected Outcome:** Confirm which service/API is failing, whether it's external or internal, and what the team should do.

## Time Window
- Use `startsAt - 10 minutes` to `startsAt + 1 hour` for all queries.
- If not available, use `now - 30 minutes`.

## Infrastructure Reference
- **Elasticsearch app logs index:** `app-logs-YYYY-MM-DD`
- **App log format:** `TIMESTAMP LEVEL> @pod-name [requestId-UUID] |> error message`
- **PT services namespace:** `atlas`
- **Key PT pods:** `bap-app-backend`, `public-transport-backend`, `gtfs-in-memory-server`

---

## Alert: CMRLAPIGivingErrors / CrisAPIGivingErrors / PTExternalAPIErrorsIncreased

These fire when SRE Platform's calls to external PT APIs (CMRL = Chennai Metro Rail, CRIS = Indian Railways) are failing.

### Step 1: Check Error Rate and Duration
```
# Check if alert is still firing or resolved
kubectl get events -n atlas --sort-by=.lastTimestamp | tail -20
```

### Step 2: Check Which Pods Are Calling the API
```
kubectl get pods -n atlas | grep -iE "public-transport|pt-backend|beckn-app"
kubectl top pods -n atlas | sort -k3 -rn | head -20
```

### Step 3: Check App Logs for External API Errors
```
timeout 30 stern -n atlas bap-app-backend --since 30m 2>/dev/null | grep -iE "cmrl|cris|external|5[0-9][0-9]|timeout|refused|connect" | tail -100
timeout 30 stern -n atlas public-transport-backend --since 30m 2>/dev/null | grep -iE "error|5[0-9][0-9]|timeout|refused|cmrl|cris" | tail -100
```

### Step 4: Search Elasticsearch for PT API Errors
Search `app-logs-<today>` for the alert window:
```json
{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "now-1h"}}},
        {"bool": {
          "should": [
            {"match": {"message": "CMRL"}},
            {"match": {"message": "CRIS"}},
            {"match": {"message": "external api"}},
            {"match": {"message": "connection refused"}},
            {"match": {"message": "timeout"}}
          ],
          "minimum_should_match": 1
        }}
      ]
    }
  }
}
```

### Step 5: Determine Our Side vs External Side
- **External outage:** Errors start suddenly, affect all requests, our pods are healthy, error is `connection refused` or `5xx from upstream`
- **Our code issue:** Errors start after a deployment, only some endpoints affected, pods showing crashes/OOM
- **Network issue:** Errors on multiple external APIs simultaneously, kubectl DNS lookups failing

### Synthesis
- **All requests failing suddenly** → External provider outage. Escalate to CMRL/CRIS team. No action on our side.
- **After deployment** → Rollback candidate. Check `kubectl rollout history -n atlas`.
- **Intermittent, partial failures** → Check retry/circuit breaker config. May need tuning.
- **Connection refused** → Check egress network policies and service mesh config.

---

## Alert: GRPCDown (GRPC Notifications Dropped to Zero)

Fires when gRPC-based push notifications (driver/rider app) drop to zero.

### Step 1: Check GRPC Service Pod Health
```
kubectl get pods -n atlas | grep -iE "grpc|notif|push"
kubectl describe pod -n atlas <grpc-pod-name>
```

### Step 2: Check Recent Restarts
```
kubectl get pods -n atlas --sort-by='.status.containerStatuses[0].restartCount' | tail -20
```

### Step 3: Check GRPC Pod Logs
```
timeout 30 stern -n atlas <grpc-pod> --since 15m 2>/dev/null | grep -iE "error|exception|crash|panic|disconnect" | tail -50
```

### Step 4: Check for Network/Istio Issues
```
kubectl get events -n atlas | grep -iE "grpc|network|sidecar|istio" | tail -20
kubectl top pods -n atlas | grep -iE "grpc|notif"
```

### Synthesis
- **Pod crash loop** → Check OOMKill or config error in logs. May need memory increase.
- **Pods healthy but 0 notifications** → Check if notification queue is empty (producer issue?) or upstream disconnected.
- **Resolves in 5 min** → Likely pod restart during rolling update. Safe to watch.
- **Istio sidecar errors** → Check sidecar resource limits. Akhilesh 0DC pattern: increase sidecar CPU/memory.

---

## Alert: GIMS5xx (GTFS In Memory Server Giving 5xx)

GTFS In-Memory Server serves static transit data (routes, stops, schedules). 5xx means requests to it are failing.

### Step 1: Check GIMS Pod Health
```
kubectl get pods -n atlas | grep -iE "gtfs|gims|in-memory"
kubectl describe pod -n atlas <gims-pod>
```

### Step 2: Check GIMS Logs
```
timeout 30 stern -n atlas gtfs-in-memory-server --since 15m 2>/dev/null | grep -iE "error|5[0-9][0-9]|exception|memory|oom" | tail -50
```

### Step 3: Check Memory Usage (GIMS loads GTFS data in-memory)
```
kubectl top pods -n atlas | grep -iE "gtfs|gims"
```

### Step 4: Check for GTFS Data Issues
```
kubectl logs -n atlas <gims-pod> --tail=100 | grep -iE "load|parse|gtfs|error"
```

### Synthesis
- **OOMKilled** → GTFS dataset grew. Increase memory limit on GIMS pod.
- **Pod healthy but 5xx** → GTFS data failed to load/refresh. Check data source.
- **Resolves quickly** → Pod restarted and reloaded data. Monitor memory trend.

---

## Alert: Refunds increased

Fires when refund rate increases above threshold — indicates payment failures or ride cancellation spike.

### Step 1: Check Payment Gateway Errors
Search Elasticsearch for payment/refund errors:
```json
{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "now-1h"}}},
        {"bool": {
          "should": [
            {"match": {"message": "refund"}},
            {"match": {"message": "payment"}},
            {"match": {"message": "juspay"}},
            {"match": {"message": "cancel"}},
            {"match": {"message": "transaction"}},
            {"match": {"message": "FAILED"}}
          ],
          "minimum_should_match": 1
        }}
      ]
    }
  }
}
```

### Step 2: Check for Recent Releases Touching Payment Flow
```
kubectl rollout history deployment -n atlas | grep -i "payment\|refund\|juspay"
kubectl get events -n atlas | grep -iE "deploy\|rollout" | tail -20
```

### Step 3: Check Juspay Gateway Health (see ny-sre-sev2 runbook if JuspayGateway5xx also firing)

### Synthesis
- **Juspay errors in logs + refund spike** → Juspay gateway issue. Monitor; check Juspay status.
- **After deployment** → Code regression in payment flow. Rollback candidate.
- **Ride cancellation spike** → Check if there's a correlated service degradation causing users to cancel.
- **Brief spike, resolves** → Likely transient Juspay timeout. Safe to watch unless persists >10 min.
