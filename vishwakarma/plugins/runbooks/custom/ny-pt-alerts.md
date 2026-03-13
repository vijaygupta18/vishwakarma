# Public Transit (PT) Alerts Runbook

## Goal
- **Scope:** Public Transit integration alerts — external PT APIs, GTFS In-Memory Server (GIMS), GRPC notifications, and business metric alerts (refunds).
- **Agent Mandate:** Read-only. Identify whether the issue is on our side or external provider side, find root cause, recommend action.
- **Expected Outcome:** Confirm which service/API is failing, whether it's external or internal, and what the team should do.

## Time Window
- Use `startsAt - 10 minutes` to `startsAt + 1 hour` for all queries.
- If not available, use `now - 30 minutes`.

## Infrastructure Reference
Refer to the **Site Knowledge Base** for your cluster's specific values:
- Service namespace and pod name patterns for PT services
- App log index name and format
- Key PT service names (app backend, PT backend, GTFS in-memory server)

---

## Alert: CMRLAPIGivingErrors / CrisAPIGivingErrors / PTExternalAPIErrorsIncreased

These fire when calls to external PT APIs (e.g. metro rail, railways) are failing.

### Step 1: Check Error Rate and Duration
```
# Check if alert is still firing or resolved
kubectl get events -n <namespace> --sort-by=.lastTimestamp | tail -20
```

### Step 2: Check Which Pods Are Calling the API
```
kubectl get pods -n <namespace> | grep -iE "public-transport|pt-backend|app-backend"
kubectl top pods -n <namespace> | sort -k3 -rn | head -20
```

### Step 3: Check App Logs for External API Errors
```
timeout 30 stern -n <namespace> <app-backend-service> --since 30m 2>/dev/null | grep -iE "cmrl|cris|external|5[0-9][0-9]|timeout|refused|connect" | tail -100
timeout 30 stern -n <namespace> <pt-backend-service> --since 30m 2>/dev/null | grep -iE "error|5[0-9][0-9]|timeout|refused|cmrl|cris" | tail -100
```

### Step 4: Search Elasticsearch for PT API Errors
Search the app log index (from knowledge base) for the alert window:
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
- **All requests failing suddenly** → External provider outage. Escalate to provider team. No action on our side.
- **After deployment** → Rollback candidate. Check `kubectl rollout history -n <namespace>`.
- **Intermittent, partial failures** → Check retry/circuit breaker config. May need tuning.
- **Connection refused** → Check egress network policies and service mesh config.

---

## Alert: GRPCDown (GRPC Notifications Dropped to Zero)

Fires when gRPC-based push notifications (driver/rider app) drop to zero.

### Step 1: Check GRPC Service Pod Health
```
kubectl get pods -n <namespace> | grep -iE "grpc|notif|push"
kubectl describe pod -n <namespace> <grpc-pod-name>
```

### Step 2: Check Recent Restarts
```
kubectl get pods -n <namespace> --sort-by='.status.containerStatuses[0].restartCount' | tail -20
```

### Step 3: Check GRPC Pod Logs
```
timeout 30 stern -n <namespace> <grpc-pod> --since 15m 2>/dev/null | grep -iE "error|exception|crash|panic|disconnect" | tail -50
```

### Step 4: Check for Network/Istio Issues
```
kubectl get events -n <namespace> | grep -iE "grpc|network|sidecar|istio" | tail -20
kubectl top pods -n <namespace> | grep -iE "grpc|notif"
```

### Synthesis
- **Pod crash loop** → Check OOMKill or config error in logs. May need memory increase.
- **Pods healthy but 0 notifications** → Check if notification queue is empty (producer issue?) or upstream disconnected.
- **Resolves in 5 min** → Likely pod restart during rolling update. Safe to watch.
- **Istio sidecar errors** → Check sidecar resource limits. Increase sidecar CPU/memory.

---

## Alert: GIMS5xx (GTFS In Memory Server Giving 5xx)

GTFS In-Memory Server serves static transit data (routes, stops, schedules). 5xx means requests to it are failing.

### Step 1: Check GIMS Pod Health
```
kubectl get pods -n <namespace> | grep -iE "gtfs|gims|in-memory"
kubectl describe pod -n <namespace> <gims-pod>
```

### Step 2: Check GIMS Logs
```
timeout 30 stern -n <namespace> <gtfs-in-memory-service> --since 15m 2>/dev/null | grep -iE "error|5[0-9][0-9]|exception|memory|oom" | tail -50
```

### Step 3: Check Memory Usage (GIMS loads GTFS data in-memory)
```
kubectl top pods -n <namespace> | grep -iE "gtfs|gims"
```

### Step 4: Check for GTFS Data Issues
```
kubectl logs -n <namespace> <gims-pod> --tail=100 | grep -iE "load|parse|gtfs|error"
```

### Synthesis
- **OOMKilled** → GTFS dataset grew. Increase memory limit on GIMS pod.
- **Pod healthy but 5xx** → GTFS data failed to load/refresh. Check data source.
- **Resolves quickly** → Pod restarted and reloaded data. Monitor memory trend.

---

## Alert: Refunds increased

Fires when refund rate increases above threshold — indicates payment failures or ride cancellation spike.

### Step 1: Check Payment Gateway Errors
Search the app log index (from knowledge base) for payment/refund errors:
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
kubectl rollout history deployment -n <namespace> | grep -i "payment\|refund"
kubectl get events -n <namespace> | grep -iE "deploy\|rollout" | tail -20
```

### Step 3: Check Payment Gateway Health (see sev2 runbook if PaymentGateway5xx also firing)

### Synthesis
- **Payment gateway errors in logs + refund spike** → Payment gateway issue. Monitor; check gateway status page.
- **After deployment** → Code regression in payment flow. Rollback candidate.
- **Ride cancellation spike** → Check if there's a correlated service degradation causing users to cancel.
- **Brief spike, resolves** → Likely transient gateway timeout. Safe to watch unless persists >10 min.

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

