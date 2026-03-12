# Redis / ElastiCache Investigation Runbook

## Goal
- **Primary Objective:** Investigate Redis/ElastiCache alerts — high CPU, high memory, evictions, high connections, or replication lag.
- **Scope:** AWS ElastiCache Redis clusters for SRE Platform in <region>. Used for session caching, driver location data, ride state, and KV store.
- **Agent Mandate:** Read-only. Do not flush, delete keys, or modify any Redis configuration. Provide RCA with possible root causes for the team to act on.
- **Expected Outcome:** Identify what is stressing Redis, which service/key pattern is responsible, and what the team should do.

## Time Window Instructions
- The alert's `startsAt` field contains the **exact time the alarm fired** — use this as your investigation window start.
- For all CloudWatch and Elasticsearch queries use: `start-time = startsAt - 10 minutes`, `end-time = startsAt + 1 hour`
- If `startsAt` is not available, fall back to `now - 30 minutes`.
- Always state the time window used in your findings (e.g. "investigated 17:00–18:00 UTC").

## Infrastructure Reference
- **Redis clusters (replication groups):**
  - `main-redis-cluster` — main app cache (sessions, ride state, KV store)
  - `location-redis` — location tracking service (driver location data)
  - `utils-redis-cluster` — utility services
- **Elasticsearch app logs index:** `app-logs-YYYY-MM-DD` (e.g. `app-logs-2026-03-12`)
- **Elasticsearch endpoint:** `https://<elasticsearch-endpoint>`

## Workflow

### Step 1: List ALL Clusters and Find the Alerting One
First, get all ElastiCache replication groups in the account:
```
aws elasticache describe-replication-groups --region <region> --query 'ReplicationGroups[*].[ReplicationGroupId,Status,NodeGroups[0].PrimaryEndpoint.Address]' --output table
```

Also list individual cache clusters:
```
aws elasticache describe-cache-clusters --show-cache-node-info --region <region> --query 'CacheClusters[*].[CacheClusterId,CacheNodeType,CacheClusterStatus,Engine]' --output table
```

If an alarm is firing, identify which cluster it's for:
```
aws cloudwatch describe-alarms --state-value ALARM --region <region> --query 'MetricAlarms[*].[AlarmName,Dimensions,StateReason]' --output table
```

Note ALL replication group IDs from Step 1 — you will check CPU on all of them in Step 2.

### Step 2: Check CPU on ALL Clusters (Find Which One is High)
For **each replication group** returned in Step 1, run:
```
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name EngineCPUUtilization --dimensions Name=ReplicationGroupId,Value=<replication-group-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Average Maximum --region <region>
```

Run this for every cluster. Identify which clusters have high CPU (> 50%) or other anomalies. Focus the rest of the investigation on those.

### Step 3: Check All Key Metrics on the Affected Cluster(s)
For each affected cluster found in Step 2:
```
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name DatabaseMemoryUsagePercentage --dimensions Name=ReplicationGroupId,Value=<affected-cluster-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Average --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name Evictions --dimensions Name=ReplicationGroupId,Value=<affected-cluster-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Sum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name CurrConnections --dimensions Name=ReplicationGroupId,Value=<affected-cluster-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Maximum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name NewConnections --dimensions Name=ReplicationGroupId,Value=<affected-cluster-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Maximum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name CacheHits --dimensions Name=ReplicationGroupId,Value=<affected-cluster-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Sum --region <region>
aws cloudwatch get-metric-statistics --namespace AWS/ElastiCache --metric-name CacheMisses --dimensions Name=ReplicationGroupId,Value=<affected-cluster-id> --start-time <1h ago ISO8601> --end-time <now ISO8601> --period 300 --statistics Sum --region <region>
```

Interpret:
- `EngineCPUUtilization` — actual Redis engine CPU (use this, not CPUUtilization)
- `DatabaseMemoryUsagePercentage` — memory % of maxmemory
- `Evictions` — keys evicted due to memory pressure (> 0 = memory full)
- `CurrConnections` — current connections
- `NewConnections` — connection creation rate (spike = connection storm)
- `CacheHits` / `CacheMisses` — hit ratio = Hits / (Hits + Misses); low ratio = evictions hurting cache

### Step 3: Check Eviction Policy
`aws elasticache describe-cache-parameter-groups --region <region>`

Find the parameter group used by the alerting cluster, then:
`aws elasticache describe-cache-parameters --cache-parameter-group-name <param-group-name> --region <region> | grep -iE "maxmemory|eviction"`

If evictions > 0: Redis is full. Cache misses will increase, forcing more DB reads → cascading RDS CPU spike.

### Step 4: Correlate with Application Pods
Find all pods that use Redis across all namespaces:
`kubectl get pods -A | grep -iE "beckn|atlas|drainer|producer|backend|alloc"`

Grep their logs for Redis errors during the spike:
`timeout 30 stern -n atlas bap-app-backend --since 1h | grep -iE "redis|cache|timeout|refused|clusterdown|moved|evict|conn" | head -200`
`timeout 30 stern -n atlas bpp-backend --since 1h | grep -iE "redis|cache|timeout|refused|clusterdown|moved" | head -200`

### Step 5: Search Elasticsearch for Redis Errors
Search `app-logs-<today's date>` index for Redis errors in the last hour:
```json
{
  "size": 20,
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "now-1h"}}},
        {"bool": {
          "should": [
            {"match": {"message": "redis"}},
            {"match": {"message": "timeout"}},
            {"match": {"message": "connection refused"}},
            {"match": {"message": "CLUSTERDOWN"}},
            {"match": {"message": "MOVED"}},
            {"match": {"message": "evict"}}
          ],
          "minimum_should_match": 1
        }}
      ]
    }
  },
  "_source": ["message", "@timestamp"]
}
```
Look inside the `message` field (it's JSON) for the `log` key which has the actual error text. Correlate error timestamps with the CloudWatch metric spikes from Step 2.

### Step 6: Check for Connection Storm
High `CurrConnections` or `NewConnections` spike:
- Check if HPA scaled up a service recently (more pods = more Redis connections):
  `kubectl get events -A --sort-by='.lastTimestamp' | grep -iE "scaled|replica|hpa" | tail -20`
- Check HPA status: `kubectl get hpa -A`

### Step 7: Check for Expensive Commands
If Redis CPU is high, look for `KEYS *`, `SMEMBERS` on large sets, `SORT`, `LRANGE` on large lists:
`timeout 30 stern -n atlas bap-app-backend --since 1h | grep -iE "KEYS|SMEMBERS|LRANGE|SORT|SCAN" | head -50`

## Synthesize Findings

- **High CPU + expensive command in logs** → `KEYS *` or large set operation. Report the command and service calling it.
- **High memory + evictions > 0** → Cache full, keys not expiring. Report memory%, eviction count, and which service is storing large/unbounded keys.
- **Evictions + low cache hit ratio + RDS CPU spike** → Cascading failure: Redis full → cache misses → DB overload. Redis is the root cause.
- **High CurrConnections + recent HPA scale-up** → Connection storm from new pods. Report the service and pod count increase.
- **Connection refused in app logs** → Redis maxclients limit hit. Report current connections vs limit.
- **Replication lag + high WriteIOPS** → Heavy write load on primary. Report write-heavy service.

## Possible Fixes (for team to action)
- For high memory/evictions: increase node size or add a shard
- Set TTLs on keys that are growing unbounded
- Replace `KEYS *` with cursor-based `SCAN`
- Implement connection pooling in the service
- Review `maxmemory-policy` — `allkeys-lru` is safer than `noeviction`
