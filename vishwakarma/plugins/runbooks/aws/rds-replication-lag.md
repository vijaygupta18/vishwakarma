# RDS Replication Lag Investigation Runbook

## Goal
Investigate AWS RDS replication lag alerts on Aurora PostgreSQL. Determine:
1. **Which type of replication** is lagging — logical replication slots (AWS→GCP cross-cloud) vs Aurora read replica lag
2. Which specific instance/slot is affected
3. Root cause (stale slot, subscriber failure, write surge, WAL growth)
4. Whether user-facing impact exists
5. Confidence level in the root cause
6. Whether immediate action is needed (stale slot dropping requires DBA)

**Agent Mandate:** Read-only. Do not drop replication slots, kill queries, or modify any DB/RDS settings.

## CRITICAL: Two Different Types of Replication Lag

This cluster uses TWO completely separate replication mechanisms. **Do not confuse them.**

| | Logical Replication Slots (AWS→GCP) | Aurora Read Replica Lag |
|---|---|---|
| **What** | PostgreSQL logical replication slots sending WAL to GCP subscriber | Aurora's internal storage-level replication to read replicas |
| **Metric** | `OldestReplicationSlotLag` (on **writers**) | `AuroraReplicaLag` (on **readers**) |
| **Unit** | **SECONDS** (divide by 86400 for days) | **MILLISECONDS** |
| **Normal range** | 0-300 seconds (stale slots can show 100+ days = 8,640,000+ seconds) | <20ms |
| **Alarming range** | >3600 seconds (1 hour) | >100ms |
| **Cause of lag** | GCP subscriber disconnected/crashed, slot not dropped | Heavy writes on writer, long-running queries on reader |
| **Fix** | Drop stale slot (DBA), fix GCP subscriber | Kill long query on reader, reduce writer load |
| **Where to check** | Writer instances (<writer-instance-1>, <writer-instance-2>) | Reader instances (<reader-instance-1>, <reader-instance-2>) |

## CRITICAL: Unit Conversions — Previous RCAs Got This Wrong

| Metric | Raw Unit | To convert |
|---|---|---|
| `OldestReplicationSlotLag` | **SECONDS** | ÷ 86400 = days, ÷ 3600 = hours |
| `ReplicationSlotDiskUsage` | **BYTES** | ÷ 1048576 = MB, ÷ 1073741824 = GB |
| `TransactionLogsDiskUsage` | **BYTES** | ÷ 1048576 = MB, ÷ 1073741824 = GB |
| `AuroraReplicaLag` | **MILLISECONDS** | ÷ 1000 = seconds |
| `AuroraReplicaLagMaximum` | **MILLISECONDS** | ÷ 1000 = seconds |

**WARNING:** `ReplicationSlotDiskUsage` returns BYTES, not MB. A value of 16,984 means 16,984 bytes (~16 KB), NOT 16,984 MB. Always convert explicitly.

## Time Window
- Use `startsAt` from the alert as your investigation anchor
- Query window: `startsAt - 30 minutes` to `startsAt + 1 hour`
- For logical replication issues, also check a **7-day trend** — stale slots degrade gradually
- If `startsAt` not available, use `now - 30 minutes`

## Infrastructure Reference
Refer to the **Site Knowledge Base** for your cluster's specific values:
- Aurora cluster identifiers: `<driver-cluster-id>` (driver), `<customer-cluster-id>` (customer)
- Alert name → cluster mapping
- Elasticsearch endpoint + app log index name
- Business-critical Prometheus metrics

---

## IMPORTANT: Tool Routing
- **RDS metrics (replication lag, disk usage, IOPS)**: Use `aws cloudwatch get-metric-statistics` via bash — NOT Prometheus. RDS metrics are NOT in Prometheus.
- **Instance discovery**: Use `aws rds describe-db-instances` to find ALL instances including autoscaled replicas — NEVER rely on hardcoded instance lists.
- **Replication slot diagnostics**: Use `db_query` for `pg_replication_slots` and `pg_stat_replication`
- **Business impact (5xx, latency)**: Use `prometheus_query_range` — these ARE application metrics in Prometheus.
- **Application logs**: Use `elasticsearch_search`

---

## Step 0: Alert Freshness Check + Replication Type Identification

Before investigating, determine: (a) is this real, (b) which replication type is involved.

**0a — Identify affected instance from alert:**
Extract the instance identifier from the alert. Determine if it is a **writer** or **reader** — this tells you which replication type to focus on:
- Writer instance → likely **logical replication slot** issue (`OldestReplicationSlotLag`)
- Reader instance → likely **Aurora replica lag** issue (`AuroraReplicaLag`)

**0b — Discover all instances and their roles:**
```
aws rds describe-db-clusters --db-cluster-identifier <cluster-id> --region <region> \
  --query 'DBClusters[0].DBClusterMembers[*].[DBInstanceIdentifier,IsClusterWriter]' --output table
```

**0c — Check current logical replication slot lag on all writers:**
```
for instance in <writer-instances>; do
  echo "=== $instance ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name OldestReplicationSlotLag \
    --dimensions Name=DBInstanceIdentifier,Value=$instance \
    --start-time $(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%SZ) --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
    --period 60 --statistics Average Maximum --region <region> --output json \
    | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average)s (\(.Average/86400 | floor) days) max=\(.Maximum)s (\(.Maximum/86400 | floor) days)"'
done
```

**0d — Check current Aurora replica lag on all readers:**
```
for instance in <reader-instances>; do
  echo "=== $instance ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name AuroraReplicaLag \
    --dimensions Name=DBInstanceIdentifier,Value=$instance \
    --start-time $(date -u -d '10 minutes ago' +%Y-%m-%dT%H:%M:%SZ) --end-time $(date -u +%Y-%m-%dT%H:%M:%SZ) \
    --period 60 --statistics Average Maximum --region <region> --output json \
    | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average)ms max=\(.Maximum)ms"'
done
```

**Interpret:**
- **OldestReplicationSlotLag > 8,640,000 seconds (100+ days)** → STALE LOGICAL REPLICATION SLOT — proceed to Steps 1-3
- **OldestReplicationSlotLag > 3,600 seconds (1+ hours) but growing** → ACTIVE SLOT LAGGING — proceed to Steps 1-3
- **OldestReplicationSlotLag < 300 seconds** → Logical replication is healthy, check Aurora replica lag
- **AuroraReplicaLag > 100ms** → AURORA REPLICA LAG — proceed to Steps 4-5
- **Both metrics normal** → FALSE ALARM — proceed to Step 6 (verification only)

---

## Step 1: Logical Replication Slot Deep Dive (Writers Only)

**Run this step if OldestReplicationSlotLag is elevated.** Run all commands in parallel.

**1a — Discover all instances in the cluster:**
```
aws rds describe-db-instances --region <region> \
  --query 'DBInstances[?DBClusterIdentifier==`<cluster-id>`].[DBInstanceIdentifier,DbiResourceId,DBInstanceClass,DBInstanceStatus]' \
  --output table
```

**1b — OldestReplicationSlotLag trend (30-minute window around alert):**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name OldestReplicationSlotLag \
  --dimensions Name=DBInstanceIdentifier,Value=<writer-instance> \
  --start-time <startsAt-30min> --end-time <startsAt+1h> \
  --period 60 --statistics Average Minimum Maximum --region <region> --output json \
  | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): min=\(.Minimum/86400 | . * 100 | floor / 100) days, avg=\(.Average/86400 | . * 100 | floor / 100) days, max=\(.Maximum/86400 | . * 100 | floor / 100) days"'
```

**NOTE on Min/Max spread:** If Minimum and Maximum differ significantly (e.g., min=2 days, max=200 days), this means there are **multiple replication slots with different lag levels**. The Maximum reflects the most stale slot. Proceed to Step 2 to identify individual slots.

**1c — ReplicationSlotDiskUsage (BYTES — convert to MB/GB):**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name ReplicationSlotDiskUsage \
  --dimensions Name=DBInstanceIdentifier,Value=<writer-instance> \
  --start-time <startsAt-30min> --end-time <startsAt+1h> \
  --period 60 --statistics Average Maximum --region <region> --output json \
  | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average) bytes (\(.Average/1073741824 | . * 100 | floor / 100) GB) max=\(.Maximum) bytes (\(.Maximum/1073741824 | . * 100 | floor / 100) GB)"'
```

**1d — TransactionLogsDiskUsage (BYTES — indicates WAL retention):**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name TransactionLogsDiskUsage \
  --dimensions Name=DBInstanceIdentifier,Value=<writer-instance> \
  --start-time <startsAt-30min> --end-time <startsAt+1h> \
  --period 60 --statistics Average Maximum --region <region> --output json \
  | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average) bytes (\(.Average/1073741824 | . * 100 | floor / 100) GB) max=\(.Maximum) bytes (\(.Maximum/1073741824 | . * 100 | floor / 100) GB)"'
```

**1e — 7-day trend for OldestReplicationSlotLag (is it growing or stable?):**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name OldestReplicationSlotLag \
  --dimensions Name=DBInstanceIdentifier,Value=<writer-instance> \
  --start-time <startsAt-7days> --end-time <startsAt> \
  --period 3600 --statistics Average Maximum --region <region> --output json \
  | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average/86400 | . * 100 | floor / 100) days, max=\(.Maximum/86400 | . * 100 | floor / 100) days"'
```

**1f — Writer health metrics (to rule out writer overload as contributing factor):**
```
for metric in WriteIOPS WriteThroughput WriteLatency CPUUtilization; do
  echo "=== $metric ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name $metric \
    --dimensions Name=DBInstanceIdentifier,Value=<writer-instance> \
    --start-time <startsAt-30min> --end-time <startsAt+1h> \
    --period 60 --statistics Average Maximum --region <region> --output json \
    | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average) max=\(.Maximum)"'
done
```

**1g — AuroraReplicaLagMaximum on writer (sanity check for Aurora-level replication):**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name AuroraReplicaLagMaximum \
  --dimensions Name=DBInstanceIdentifier,Value=<writer-instance> \
  --start-time <startsAt-30min> --end-time <startsAt+1h> \
  --period 60 --statistics Average Maximum --region <region> --output json \
  | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average)ms max=\(.Maximum)ms"'
```

**1h — Aurora cluster events (failover, maintenance):**
```
aws rds describe-events --source-type db-instance --duration 120 --region <region> \
  --query 'Events[].[SourceIdentifier,Date,Message]' --output table
```

**1i — Check the CloudWatch alarm definition and history:**
```
aws cloudwatch describe-alarms --alarm-names "<alarm-name-from-alert>" --region <region> \
  --query 'MetricAlarms[*].[AlarmName,Dimensions,StateReason,StateUpdatedTimestamp,StateValue,Threshold]' --output table
```
```
aws cloudwatch describe-alarm-history --alarm-name "<alarm-name-from-alert>" --region <region> \
  --start-date <startsAt-24h> --end-date <startsAt+1h> \
  --history-item-type StateUpdate \
  --query 'AlarmHistoryItems[].[Timestamp,HistorySummary]' --output table
```

**After Step 1:** Fill out this characterization:
```
Writer instance: <name>
OldestReplicationSlotLag: <value> seconds = <days> days
  - Min/Max spread: <min>/<max> (multiple slots if different)
  - 7-day trend: STABLE / GROWING / FLUCTUATING
ReplicationSlotDiskUsage: <value> bytes = <MB> MB = <GB> GB
TransactionLogsDiskUsage: <value> bytes = <MB> MB = <GB> GB
Writer CPU: <value>%
Writer WriteIOPS: <value>
Aurora Replica Lag (from writer): <value> ms
Pattern: STALE SLOT / ACTIVE LAGGING / WRITE SURGE / NORMAL
```

---

## Step 2: Replication Slot Diagnostics via Direct SQL

**Run this step if Step 1 shows elevated OldestReplicationSlotLag.** Use `db_query` on the writer's PostgreSQL connection (bap_pg for customer, bpp_pg for driver).

Run `learnings_read(database)` first to get connection details.

**2a — List all replication slots with individual lag:**
```sql
SELECT slot_name, plugin, slot_type, active, restart_lsn, confirmed_flush_lsn,
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)) AS lag_size,
       pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn) AS lag_bytes
FROM pg_replication_slots ORDER BY lag_bytes DESC NULLS LAST;
```

**Interpret:**
- **`active = false`** → Subscriber is DISCONNECTED. This slot is stale and retaining WAL. This is the most common cause of 100+ day lag.
- **`active = true` but `lag_bytes` is large and growing** → Subscriber is connected but can't keep up. Check GCP subscriber health.
- **`active = true` and `lag_bytes` is small** → Slot is healthy. If OldestReplicationSlotLag metric is still high, there's likely ANOTHER stale slot.
- **Multiple slots** → The OldestReplicationSlotLag metric reflects the WORST slot. Identify which specific slot(s) are the problem.

**2b — Check active replication connections (who is consuming the slots):**
```sql
SELECT pid, application_name, client_addr, state, sent_lsn, write_lsn, flush_lsn, replay_lsn,
       pg_size_pretty(pg_wal_lsn_diff(sent_lsn, replay_lsn)) AS replay_lag
FROM pg_stat_replication ORDER BY replay_lag DESC NULLS LAST;
```

**Interpret:**
- **No rows for a logical slot** → Subscriber is not connected. Confirms stale slot.
- **`state = 'streaming'`** → Active, healthy connection.
- **`state = 'catchup'`** → Subscriber is reconnecting and catching up. May resolve on its own.
- **Large `replay_lag`** → Subscriber is connected but falling behind.
- **`client_addr` tells you which GCP instance** is the subscriber — useful for debugging GCP-side issues.

**2c — WAL retention and settings:**
```sql
SHOW wal_level;
```
```sql
SHOW max_replication_slots;
```
```sql
SHOW max_slot_wal_keep_size;
```

**Interpret `max_slot_wal_keep_size`:**
- If set to `-1` (default) → NO LIMIT on WAL retention. A stale slot will retain WAL forever, growing TransactionLogsDiskUsage until storage is full.
- If set to a value → WAL will be truncated even if the slot needs it, causing the slot to become invalid (subscriber will need full resync).

**2d — Current WAL position and throughput:**
```sql
SELECT pg_current_wal_lsn(), pg_walfile_name(pg_current_wal_lsn()),
       pg_size_pretty(pg_wal_lsn_diff(pg_current_wal_lsn(), '0/0')) AS total_wal_written;
```

---

## Step 3: Assess Storage Risk from Stale Slots

**Run this step if Step 2 identified stale/inactive slots.**

**3a — Current storage utilization:**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name FreeLocalStorage \
  --dimensions Name=DBInstanceIdentifier,Value=<writer-instance> \
  --start-time <startsAt-30min> --end-time <startsAt+1h> \
  --period 60 --statistics Average Minimum --region <region> --output json \
  | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average) bytes (\(.Average/1073741824 | . * 100 | floor / 100) GB)"'
```

**3b — Storage trend (7-day):**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name TransactionLogsDiskUsage \
  --dimensions Name=DBInstanceIdentifier,Value=<writer-instance> \
  --start-time <startsAt-7days> --end-time <startsAt> \
  --period 3600 --statistics Average --region <region> --output json \
  | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): \(.Average) bytes (\(.Average/1073741824 | . * 100 | floor / 100) GB)"'
```

**Risk assessment:**
- **TransactionLogsDiskUsage growing AND FreeLocalStorage shrinking** → URGENT: stale slot is consuming storage. If not dropped, writer will run out of local storage.
- **TransactionLogsDiskUsage stable** → Slot is stale but WAL has been truncated (max_slot_wal_keep_size is set) or write volume is low. Less urgent.
- **FreeLocalStorage < 5 GB** → CRITICAL: storage exhaustion imminent. Escalate to DBA immediately.

---

## Step 4: Aurora Read Replica Lag Investigation (Readers Only)

**Run this step if AuroraReplicaLag on readers is > 100ms.** This is separate from logical replication slots.

Run all commands in parallel:

**4a — AuroraReplicaLag across all readers:**
```
for instance in <all-reader-instances>; do
  echo "=== $instance ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name AuroraReplicaLag \
    --dimensions Name=DBInstanceIdentifier,Value=$instance \
    --start-time <startsAt-30min> --end-time <startsAt+1h> \
    --period 60 --statistics Average Maximum --region <region> --output json \
    | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average)ms max=\(.Maximum)ms"'
done
```

**4b — Writer WriteIOPS (heavy writes cause reader lag):**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name WriteIOPS \
  --dimensions Name=DBInstanceIdentifier,Value=<writer-instance> \
  --start-time <startsAt-30min> --end-time <startsAt+1h> \
  --period 60 --statistics Average Maximum --region <region> --output json \
  | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average) max=\(.Maximum)"'
```

**4c — Reader CPU (long-running queries on reader hold read locks, blocking apply):**
```
for instance in <all-reader-instances>; do
  echo "=== $instance ===" && \
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
    --dimensions Name=DBInstanceIdentifier,Value=$instance \
    --start-time <startsAt-30min> --end-time <startsAt+1h> \
    --period 60 --statistics Average Maximum --region <region> --output json \
    | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average | floor)% max=\(.Maximum | floor)%"'
done
```

**4d — Long-running queries on readers (these can block replication apply):**
```sql
SELECT pid, now() - query_start AS duration, state, wait_event_type, wait_event, left(query, 200) as query
FROM pg_stat_activity
WHERE state = 'active' AND query NOT LIKE '%pg_stat_activity%'
ORDER BY duration DESC LIMIT 20;
```

**4e — 7-day baseline for AuroraReplicaLag:**
```
aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name AuroraReplicaLag \
  --dimensions Name=DBInstanceIdentifier,Value=<reader-instance> \
  --start-time <startsAt-7days-15min> --end-time <startsAt-7days+1h> \
  --period 300 --statistics Average Maximum --region <region> --output json \
  | jq -r '.Datapoints | sort_by(.Timestamp) | .[] | "\(.Timestamp): avg=\(.Average)ms max=\(.Maximum)ms"'
```

---

## Step 5: Check Business Impact (Run in Parallel with Steps 1-4)

**5a — 5xx error rate (Prometheus):**
- query: `sum by(service,handler)(rate(http_request_duration_seconds_count{handler!="/v1/",status_code=~"^5.."}[1m]))`
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**5b — P99 latency (Prometheus):**
- query: `histogram_quantile(0.99, sum(rate(http_request_duration_seconds_bucket[5m])) by (le, service))`
- start: `<startsAt - 10m>`, end: `<startsAt + 1h>`, step: `1m`

**5c — ALB 5xx from CloudWatch:**
```
aws cloudwatch get-metric-statistics --namespace AWS/ApplicationELB \
  --metric-name HTTPCode_Target_5XX_Count \
  --dimensions Name=LoadBalancer,Value=<alb-arn-from-knowledge-base> \
  --start-time <startsAt-10min> --end-time <startsAt+1h> \
  --period 60 --statistics Sum --region <region> --output json \
  | jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): \(.Sum) 5xx"'
```

**5d — Key business metrics (check Site Knowledge Base for deployment-specific metrics):**
- Use business-critical metrics from knowledge base (e.g., conversion rates, transaction counts)
- start: `<startsAt - 30m>`, end: `<startsAt + 1h>`, step: `5m`

**Impact assessment — fill this out:**
```
5xx rate:          NONE / LOW (<10/min) / MEDIUM (10-100/min) / HIGH (>100/min)
P99 latency:       NORMAL / DEGRADED (>3s) / SEVERE (>10s)
Business:          STABLE / DEGRADED — <which metrics affected>
User impact:       YES / NO — <describe affected user operations>
Replication impact: Is GCP-served traffic using stale data? YES/NO
                    If logical slot lag is 100+ days, GCP data is 100+ days stale for that slot.
```

---

## Step 6: Correlate — Recent Changes and External Factors

**Run this step to identify what changed that could have caused or contributed to the lag.**

**6a — Recent deploys:**
```
kubectl get replicasets -n <namespace> --sort-by=.metadata.creationTimestamp -o wide | tail -15
```

**6b — Aurora events (failover, maintenance, scaling):**
```
aws rds describe-events --source-type db-instance --duration 1440 --region <region> \
  --query 'Events[].[SourceIdentifier,Date,Message]' --output table
```

**6c — Check if GCP subscriber service is running (if you have access to GCP):**
Look in ES or Kubernetes logs for the GCP-side logical replication subscriber for errors:
```json
{
  "index": "<app-log-index-from-knowledge-base>",
  "size": 20,
  "sort": [{"@timestamp": "desc"}],
  "query": {
    "bool": {
      "must": [
        {"range": {"@timestamp": {"gte": "<startsAt-1h>", "lte": "<startsAt+1h>"}}},
        {"bool": {
          "should": [
            {"match": {"message": "replication"}},
            {"match": {"message": "logical"}},
            {"match": {"message": "subscriber"}},
            {"match": {"message": "slot"}},
            {"match": {"message": "wal_receiver"}}
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

## Synthesis — Hypothesis Verification Matrix

**MANDATORY: Work through EVERY hypothesis below. For each one, state CONFIRMED / RULED OUT / INCONCLUSIVE with specific evidence.**

### Hypothesis 1: Stale/Inactive Logical Replication Slot
**Check:** `pg_replication_slots` shows `active = false` for one or more slots + `OldestReplicationSlotLag` is 100+ days
**Verify:** No corresponding entry in `pg_stat_replication` for that slot. The GCP subscriber is disconnected.
**Rule out:** If ALL slots show `active = true` → NOT this
**Severity:** LOW urgency (data is stale but not causing immediate outage) UNLESS TransactionLogsDiskUsage is growing dangerously
**Confidence if confirmed:** HIGH
**Fix:** Drop the stale slot — **requires DBA approval**: `SELECT pg_drop_replication_slot('<slot_name>');`

### Hypothesis 2: Active Slot Lagging (Subscriber Can't Keep Up)
**Check:** `pg_replication_slots` shows `active = true` but `lag_bytes` is large and growing. `pg_stat_replication` shows `state = 'streaming'` but `replay_lag` is significant.
**Verify:** Writer WriteIOPS/WriteThroughput is high, subscriber is connected but falling behind. Check if GCP subscriber host has CPU/memory/disk issues.
**Rule out:** If `lag_bytes` is small or stable → NOT this. If `active = false` → this is Hypothesis 1 instead.
**Confidence if confirmed:** HIGH
**Fix:** Investigate GCP subscriber health. Increase `wal_sender` resources if applicable. Reduce write load on writer if possible.

### Hypothesis 3: Heavy Write Load on Writer
**Check:** Writer WriteIOPS and WriteThroughput surged > 2x baseline at alert time. Both logical replication slot lag and Aurora replica lag increased simultaneously.
**Verify:** Performance Insights shows heavy write queries. All downstream replication (logical + Aurora) fell behind together.
**Rule out:** If WriteIOPS is normal/baseline → NOT this
**Confidence if confirmed:** HIGH
**Fix:** Identify and optimize the heavy write query. If it's a batch job, schedule for off-peak.

### Hypothesis 4: Aurora Replica Lag Spike (Reader-Specific)
**Check:** `AuroraReplicaLag` on one or more readers > 100ms. `OldestReplicationSlotLag` on writer is normal (< 300 seconds).
**Verify:** Long-running query on the reader in `pg_stat_activity` holding read locks that block replication apply. OR writer WriteIOPS is high causing apply backlog.
**Rule out:** If `AuroraReplicaLag` < 20ms on all readers → NOT this
**Confidence if confirmed:** HIGH
**Fix:** Kill the long-running query on the reader (requires DBA). If caused by write surge, will self-resolve when writes decrease.

### Hypothesis 5: WAL Disk Growth (Storage Risk)
**Check:** `TransactionLogsDiskUsage` is growing steadily over 7-day trend. `ReplicationSlotDiskUsage` is non-trivial (> 1 GB). `FreeLocalStorage` is declining.
**Verify:** A stale slot with `active = false` is retaining WAL. `max_slot_wal_keep_size` is `-1` (no limit).
**Rule out:** If `TransactionLogsDiskUsage` is stable and `FreeLocalStorage` > 20 GB → storage is not at risk
**Severity:** Can escalate to CRITICAL if storage fills up — writer will become read-only
**Confidence if confirmed:** HIGH
**Fix:** Drop the stale slot (DBA). Set `max_slot_wal_keep_size` to a safe value to prevent unbounded WAL retention.

### Hypothesis 6: Network/Connectivity Issue (Fluctuating Lag)
**Check:** `OldestReplicationSlotLag` Min and Max within the same 5-minute window differ wildly (e.g., min=2 days, max=200 days). This indicates **multiple slots with different health**, NOT a single slot fluctuating.
**Verify:** `pg_replication_slots` shows multiple slots — some active and healthy, some stale. The Min reflects the best slot, Max reflects the worst.
**Rule out:** If only one replication slot exists → the fluctuation is real network instability. If Min ≈ Max → all slots have similar lag.
**Confidence if confirmed:** MEDIUM
**Fix:** Identify which specific slots are stale vs healthy (Step 2a). Address stale slots individually.

### Hypothesis 7: Normal / False Alarm
**Check:** `OldestReplicationSlotLag` is < 300 seconds AND `AuroraReplicaLag` is < 20ms on all readers. All metrics within normal range.
**Verify:** Alarm threshold may be too sensitive. Check alarm definition — what threshold triggered it?
**Rule out:** If any lag metric is genuinely elevated → NOT a false alarm
**Confidence if confirmed:** HIGH (no action needed)
**Fix:** Adjust alarm threshold to match actual operational requirements.

---

## Final Verdict

After verifying all hypotheses, state:

```
## Verified Hypotheses
| # | Hypothesis | Verdict | Key Evidence |
|---|-----------|---------|--------------|
| 1 | Stale/inactive logical replication slot | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 2 | Active slot lagging (subscriber can't keep up) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 3 | Heavy write load on writer | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 4 | Aurora replica lag spike | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 5 | WAL disk growth (storage risk) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 6 | Network/connectivity (fluctuating lag) | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |
| 7 | Normal / false alarm | CONFIRMED/RULED OUT/INCONCLUSIVE | <evidence> |

## Replication Type
LOGICAL SLOT / AURORA REPLICA / BOTH / NEITHER (false alarm)

## Root Cause
<Confirmed hypothesis with full evidence chain>
<Identify the SPECIFIC slot name if logical replication, or SPECIFIC reader instance if Aurora replica>

## Confidence: HIGH / MEDIUM / LOW
<Why this confidence level — what evidence supports it, what's missing>

## Unit Verification (MANDATORY)
- OldestReplicationSlotLag raw value: <X> seconds = <X/86400> days
- ReplicationSlotDiskUsage raw value: <X> bytes = <X/1048576> MB = <X/1073741824> GB
- TransactionLogsDiskUsage raw value: <X> bytes = <X/1048576> MB = <X/1073741824> GB
- AuroraReplicaLag raw value: <X> ms = <X/1000> seconds

## Business Impact
5xx: <rate and trend>
Latency: <p99 and trend>
Users affected: <yes/no, which operations>
GCP data staleness: <if logical slot lag, how stale is GCP data>

## Immediate Fix
- If stale logical slot: "Drop replication slot '<slot_name>': SELECT pg_drop_replication_slot('<slot_name>');" — requires DBA approval. WARNING: after dropping, GCP subscriber will need full resync.
- If active slot lagging: "Check GCP subscriber health. Ensure subscriber service is running and has resources. If subscriber is healthy, consider increasing wal_sender_timeout."
- If heavy write load: "Identify and optimize heavy write query from Performance Insights. If batch job, reschedule to off-peak."
- If Aurora replica lag: "Identify and kill long-running query on reader: SELECT pg_terminate_backend(<pid>);" — requires DBA approval.
- If WAL disk growth: "URGENT: Drop stale slot to stop WAL retention. Set max_slot_wal_keep_size to prevent recurrence."
- If network/connectivity: "Multiple slots with different health — address each stale slot individually."
- If false alarm: "Adjust alarm threshold from <current> to <recommended>."

## Prevention
<What change prevents recurrence — be specific>
- For stale slots: Set up monitoring on pg_replication_slots active status, alert when active=false for >1 hour
- For WAL growth: Set max_slot_wal_keep_size to a safe limit (e.g., 100GB)
- For Aurora replica lag: Set up query timeout on reader connections to prevent long-running queries

## Needs More Investigation
YES / NO — <if YES, what specifically needs checking and by whom>
```

---

## Extended Investigation

If ALL hypotheses are INCONCLUSIVE after the above steps:
- Check if the replication slot was recently created or recreated (slot age)
- Check PostgreSQL error logs for replication-related errors: `aws rds download-db-log-file-portion`
- Check if the GCP-side subscriber has been recently restarted or reconfigured
- Verify network connectivity between AWS and GCP (VPN tunnel status, peering health)
- Check if `wal_level` is set to `logical` (required for logical replication)
- Look for recent Aurora version upgrades or maintenance windows that could have interrupted replication
- Check `pg_stat_wal` for WAL generation rate — if WAL generation is extremely high, subscribers may never catch up
