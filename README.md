# Vishwakarma

> **Autonomous SRE investigation agent.** Receives alerts, runs a multi-step agentic investigation across your entire observability stack, and posts a structured RCA to Slack with a PDF report — no human needed.

---

## Architecture Overview

```mermaid
graph TB
    subgraph Inputs["🔔 Alert Sources"]
        AM[AlertManager<br/>Webhook]
        CW[CloudWatch<br/>Amazon Q → Slack]
        SNS[CloudWatch SNS<br/>→ Lambda]
        SLACK[Slack<br/>@oogway debug ...]
    end

    subgraph Server["⚡ Vishwakarma Server :5050"]
        API[POST /api/alertmanager]
        DEDUP[Dedup Check<br/>fingerprint hash]
        ENRICH[Pre-Enrichment<br/>4 parallel tasks]
        ENGINE[Investigation Engine<br/>Agentic Loop ≤40 steps]
    end

    subgraph Tools["🛠️ Toolsets"]
        BASH[bash<br/>kubectl · aws · stern]
        PROM[prometheus<br/>VictoriaMetrics]
        ES[elasticsearch<br/>Istio + App logs]
        HTTP[http · internet]
    end

    subgraph Outputs["📤 Outputs"]
        PDF[PDF Report]
        SPOST[Slack Post<br/>RCA + PDF thread]
        DB[(SQLite<br/>/data/vishwakarma.db)]
    end

    AM --> API
    SNS --> API
    CW -->|parse + forward| API
    SLACK -->|direct call| ENGINE

    API --> DEDUP
    DEDUP -->|new alert| ENRICH
    DEDUP -->|duplicate| SKIP[skip]
    ENRICH --> ENGINE

    ENGINE <-->|tool calls| BASH
    ENGINE <-->|tool calls| PROM
    ENGINE <-->|tool calls| ES
    ENGINE <-->|tool calls| HTTP

    ENGINE --> PDF
    ENGINE --> SPOST
    ENGINE --> DB
```

---

## Alert → RCA Flow

```mermaid
sequenceDiagram
    participant AM as AlertManager
    participant SRV as Server
    participant PRE as Pre-Enrichment
    participant LLM as LLM (open-large)
    participant TOOLS as Toolsets
    participant SLACK as Slack

    AM->>SRV: POST /api/alertmanager {firing}
    SRV->>SRV: Dedup check (fingerprint hash)
    Note over SRV: Returns 200 immediately (non-blocking)

    par Pre-enrichment (parallel)
        SRV->>PRE: kubectl pod status + events
        SRV->>PRE: SQLite prior incidents lookup
        SRV->>PRE: fast_model entity extraction
        SRV->>PRE: Runbook keyword match
    end

    PRE-->>SRV: K8s snapshot + prior context + entities + runbook

    loop Agentic Loop (≤40 steps)
        SRV->>LLM: messages + tools schema
        LLM-->>SRV: tool_calls[]

        alt has tool calls
            par Parallel execution (≤16 tools)
                SRV->>TOOLS: tool_call_1
                SRV->>TOOLS: tool_call_2
                SRV->>TOOLS: tool_call_N
            end
            TOOLS-->>SRV: results (summarized if >8KB)
            Note over SRV: Step 20: checkpoint injection
        else no tool calls
            Note over SRV: Investigation complete
        end
    end

    SRV->>SRV: generate_pdf()
    SRV->>SLACK: post RCA summary
    SRV->>SLACK: upload PDF in thread
    SRV->>SRV: save_incident() → SQLite
```

---

## Slack Bot Flow

```mermaid
flowchart TD
    MSG[Slack Event] --> TYPE{Event type?}

    TYPE -->|app_mention| STRIP[Strip @mention from text]
    TYPE -->|DM| STRIP
    TYPE -->|channel message| ISBOT{Is bot message?}

    ISBOT -->|yes| CWCHECK{Contains<br/>CloudWatch Alarm?}
    ISBOT -->|no| DROP[ignore]

    CWCHECK -->|yes| CWPARSE[parse_cloudwatch_slack_message]
    CWCHECK -->|no| DROP

    CWPARSE -->|is_firing=true| FORWARD[POST /api/alertmanager]
    CWPARSE -->|resolved / unknown| DROP

    STRIP --> CLEAN[_clean_question<br/>first non-empty line only]
    CLEAN --> CMD{Command?}

    CMD -->|help / ?| HELPTEXT[Post help text]
    CMD -->|status| STATS[Query SQLite stats]
    CMD -->|debug ...| THREAD{In a thread?}
    CMD -->|anything else| CHAT[_simple_chat<br/>fast_model, no tools]

    THREAD -->|yes| ALARM[Fetch thread parent<br/>extract alarm context]
    THREAD -->|no| INV[run_investigation]
    ALARM --> INV

    INV --> ENGINE[engine.investigate]
    ENGINE --> PDF[generate_pdf]
    PDF --> SPOST[SlackDestination.post_investigation]
    SPOST --> SAVE[save_incident → SQLite]

    CHAT --> TONE{Detect tone}
    TONE -->|casual| CRES[Casual reply]
    TONE -->|formal| FRES[Formal reply]
    TONE -->|mixed| MRES[Matched reply]
```

---

## Investigation Engine — Agentic Loop

```mermaid
flowchart TD
    START([investigate called]) --> BUILD[Build system prompt<br/>+ runbook + knowledge + history]
    BUILD --> LOOP{Step ≤ max_steps?}

    LOOP -->|yes| COMPACT{Context > 80%<br/>of max_tokens?}
    COMPACT -->|yes| COMPACTION[Compact: summarise old<br/>turns with fast_model]
    COMPACT -->|no| LLM_CALL[Call LLM]
    COMPACTION --> LLM_CALL

    LLM_CALL --> RESP{Response has<br/>tool_calls?}

    RESP -->|no tool calls| DONE([Return LLMResult<br/>with final RCA])

    RESP -->|has tool calls| GUARD{Loop guard:<br/>same tool+params<br/>already called?}
    GUARD -->|blocked| SKIP[Skip duplicate call]
    GUARD -->|allowed| EXEC

    SKIP --> LOOP

    EXEC[Execute tools in parallel<br/>ThreadPoolExecutor 16 workers] --> RESULTS[Collect results]
    RESULTS --> LARGE{Output > 8KB?}
    LARGE -->|yes| SUMMARISE[Summarise with fast_model]
    LARGE -->|no| APPEND
    SUMMARISE --> APPEND[Append to messages]

    APPEND --> STEP20{Step == 20<br/>and not checkpointed?}
    STEP20 -->|yes| CHECKPOINT[Inject checkpoint message:<br/>RCA now or state what is missing]
    STEP20 -->|no| LOOP
    CHECKPOINT --> LOOP

    LOOP -->|exceeded| FORCE([Force final answer])
```

---

## Pre-Enrichment (Before Every Investigation)

```mermaid
flowchart LR
    ALERT[Alert received] --> PAR

    subgraph PAR[Run in parallel]
        K8S["🔍 kubectl\npod status + warning events\n+ recent replicasets"]
        PRIOR["📚 SQLite lookup\nlast 3 investigations\nof this alert"]
        ENTITY["⚡ fast_model\nextract: service, namespace\nimpact, key metric"]
        RUNBOOK["📖 Runbook match\nkeyword → agents.json\n→ LLM fallback"]
    end

    PAR --> MERGE[Merge into extra_system_prompt]
    MERGE --> ENGINE[Investigation Engine]
```

---

## Runbook Matching

```mermaid
flowchart TD
    ALERT[Alert name] --> KW{Keyword match<br/>in agents.json?}

    KW -->|match found| RB[Load runbook .md]
    KW -->|no match| LLM[LLM classification<br/>pick best from catalog]

    LLM --> FOUND{Match?}
    FOUND -->|yes| RB
    FOUND -->|no| GENERIC[Use generic investigation prompt]

    RB --> INJECT[Inject into system prompt]
    GENERIC --> INJECT

    subgraph CATALOG[agents.json — 6 runbooks]
        R1[alb-5xx-investigation<br/>keywords: alb, 5xx, elb, gateway]
        R2[rds-investigation<br/>keywords: rds, cpu, database, postgres]
        R3[redis-investigation<br/>keywords: redis, elasticache, eviction]
        R4[ny-system-alerts<br/>keywords: drainer, producer, login]
        R5[ny-sre-sev2-alerts<br/>keywords: beckn, juspay, allocator]
        R6[ny-pt-alerts<br/>keywords: cmrl, cris, gtfs, grpc]
    end
```

---

## CloudWatch Alarm Detection

```mermaid
flowchart TD
    subgraph Sources["CloudWatch → Vishwakarma (3 paths)"]
        PATH1["Path 1: SNS → Lambda\nlambda/handler.py\nsns_to_alertmanager()"]
        PATH2["Path 2: Amazon Q → Slack\nbot detects 'CloudWatch Alarm'\nin message or attachments"]
        PATH3["Path 3: AlertManager\ndirect webhook from\nVMAlertManager"]
    end

    PATH1 -->|POST| API
    PATH2 --> PARSE["parse_cloudwatch_slack_message()\n① Amazon Q format\n   CloudWatch Alarm | Name | Region | Account\n② Direct format\n   ALARM: 'name' in region"]
    PATH3 -->|POST| API

    PARSE -->|is_firing=true| FWD["Forward to\nPOST localhost:5050/api/alertmanager"]
    PARSE -->|is_firing=false or OK| DROP[drop]

    FWD --> API["/api/alertmanager\n→ Investigation Engine"]
```

---

## Data Model & Storage

```mermaid
erDiagram
    incidents {
        string id PK
        string title
        string source
        string severity
        string status
        text question
        text analysis
        json tool_outputs
        json meta
        json labels
        datetime created_at
        datetime updated_at
        string slack_ts
        string pdf_path
    }

    oracle_sessions {
        string id PK
        string title
        json messages
        datetime created_at
        datetime updated_at
    }

    dedup_state {
        string fingerprint PK
        string incident_id
        datetime expires_at
    }

    incidents ||--o{ dedup_state : "fingerprint links"
    oracle_sessions ||--o{ incidents : "session history"
```

---

## How It Works

Every investigation follows a structured protocol enforced by the system prompt:

1. **Plan** — `todo_write` with every step before touching any tool
2. **Recon (parallel)** — fire all independent tool calls simultaneously (metrics, logs, K8s events, AWS CLI)
3. **Hypotheses** — state top 3 before running more tools; eliminate with evidence
4. **Five Whys** — drill past symptoms to actual root cause
5. **Checkpoint at step 20** — LLM must decide: write RCA now or state exactly what's still missing
6. **Structured RCA** — Root Cause · Confidence (HIGH/MEDIUM/LOW) · Evidence Chain · Immediate Fix · Prevention

---

## Available Toolsets

Toolsets are enabled/disabled in `config.yaml`. The agent uses only what's enabled.

### Infrastructure

| Toolset | Description |
|---------|-------------|
| `bash` | Run shell commands — `kubectl`, `aws`, `stern`, `jq`, `grep`. Allowlist/blocklist controlled per deployment. Primary tool for K8s and AWS investigation. |
| `kubernetes` | Native K8s API tools (pod status, events, logs). Disabled by default — `bash` with `kubectl` is preferred. |
| `kubernetes_logs` | Fetch pod logs via K8s API. Disabled by default — use `stern` via `bash`. |
| `docker` | Inspect Docker containers, images, logs, and resource usage. |
| `helm` | Inspect Helm releases, chart history, and deployed values. |
| `argocd` | Query ArgoCD application sync status, health, and rollout history. |
| `cilium` | Diagnose Cilium CNI — endpoint health, network policies, Hubble flows. |
| `aks` | Query Azure Kubernetes Service clusters via `az` CLI. |

### Observability & Metrics

| Toolset | Description |
|---------|-------------|
| `prometheus` | Query Prometheus/VictoriaMetrics — instant and range queries. Always use this, never `http_get` for metrics. |
| `grafana` | Query Grafana dashboards, panels, and annotations (also Loki). |
| `datadog` | Query Datadog metrics and monitors. |
| `newrelic` | Query New Relic metrics and alerts. |
| `coralogix` | Search Coralogix logs using DataPrime or Lucene syntax. |

### Logs & Search

| Toolset | Description |
|---------|-------------|
| `elasticsearch` | Search Elasticsearch/OpenSearch logs using Query DSL. Used for app logs, Istio access logs, error traces. |
| `http` | HTTP GET to external URLs. Only for external endpoints — never for internal metrics or logs. |
| `internet` | DNS lookup and basic network diagnostics. |

### Databases & Storage

| Toolset | Description |
|---------|-------------|
| `database` | Run read-only SQL queries against PostgreSQL or MySQL. |
| `mongodb` | Query MongoDB collections (read-only). |
| `kafka` | Inspect Kafka topics, consumer groups, and lag. |

### Integrations

| Toolset | Description |
|---------|-------------|
| `servicenow_tables` | Query ServiceNow incidents and CMDB records. |
| `mcp` | Model Context Protocol — connect to MCP-compatible tool servers. |
| `todo` | Internal task tracker — used by the agent to plan and track investigation steps. |

### Tool Routing Rules

```
Metrics / PromQL  →  prometheus_query or prometheus_query_range  (NEVER http_get)
Log search        →  elasticsearch_search or loki_query           (NEVER http_get)
K8s / AWS / CLI   →  bash tool
External URLs     →  http_get
```

---

## Features

- **Runbook routing** — per-alert runbooks matched by keyword, then LLM classification fallback
- **Pre-enrichment** — K8s pod status, warning events, prior incidents, entity extraction all run in parallel before the agentic loop starts
- **Site knowledge base** — `/data/knowledge.md` on PVC, injected into every investigation, no rebuild needed to update
- **Incident history** — SQLite stores all investigations; prior findings for recurring alerts are injected as context
- **Parallel tool execution** — up to 16 tools run simultaneously per step
- **Checkpoint at step 20** — forces LLM to evaluate evidence and write RCA or state what's missing
- **Safeguards** — identical tool+params blocked from re-running; context-aware loop termination
- **Context compaction** — long investigations auto-compact to stay within LLM context window
- **PDF reports** — full RCA with evidence chain uploaded to Slack thread
- **Slack bot** — `@oogway debug <question>` for on-demand investigations; casual questions answered with tone-matching

---

## Setup

### 1. Prerequisites

- Kubernetes cluster with `kubectl` access
- LLM API (OpenAI-compatible — set `api_base` for self-hosted)
- Slack app with Bot Token (`xoxb-`) and App Token (`xapp-`) for Socket Mode
- AWS IRSA or env var credentials for CloudWatch/RDS queries (if using AWS)

### 2. Configure

```bash
cp config.example.yaml config.yaml
```

Key fields:

```yaml
llm:
  model: openai/gpt-4o          # or any OpenAI-compatible model
  api_base: https://...          # omit for OpenAI default
  api_key: sk-...                # or set VK_API_KEY env var
  fast_model: openai/gpt-4o-mini # used for summarisation, entity extraction, compaction

cluster_name: my-cluster         # shown to LLM in every investigation
max_steps: 40                    # max agentic loop iterations
dedup_window: 300                # seconds to suppress duplicate alerts

toolsets:
  prometheus:
    enabled: true
    config:
      url: http://prometheus.monitoring.svc.cluster.local:9090

  elasticsearch:
    enabled: true
    config:
      url: http://elasticsearch.logging.svc.cluster.local:9200

  bash:
    enabled: true
    config:
      allow: [kubectl, aws, stern, jq, grep, awk, head, tail, sort, uniq]
      block: [rm, curl, wget, env, python]
```

### 3. Runbooks

Runbooks are `.md` files in `vishwakarma/plugins/runbooks/`. Each describes how to investigate a specific alert type.

**To add a runbook:**

1. Create `vishwakarma/plugins/runbooks/<category>/<alert-name>.md`

2. Register it in `plugins/agents/agents.json`:
```json
{
  "id": "my-alert-investigation",
  "description": "Investigate MyAlert — what it means and how to diagnose",
  "keywords": ["myalert", "keyword2"],
  "runbook": "../runbooks/<category>/<alert-name>.md"
}
```

**Included runbooks:**

| Runbook | Covers |
|---------|--------|
| `aws/rds-investigation.md` | RDS high CPU, connections, slow queries via Performance Insights |
| `aws/redis-investigation.md` | ElastiCache high CPU, evictions, connection storms |
| `aws/alb-5xx-investigation.md` | ALB 5xx errors — Istio logs → app logs → dependency pivot |
| `custom/ny-system-alerts.md` | Drainer lag, login rate drops, producer failures, config parse errors |
| `custom/ny-sre-sev2-alerts.md` | SEV2: 5xx errors, external gateway failures, ride-to-search ratio |
| `custom/ny-pt-alerts.md` | Public transit API failures, GIMS 5xx, GRPC down, refund spikes |

### 4. Site Knowledge Base

Create `/data/knowledge.md` on your PVC with cluster-specific context for every investigation:

```markdown
## RDS Instances (region: us-east-1)
my-app-writer (writer), my-app-reader-1 (reader) — aurora-postgresql 14

## Alert → Instance Mapping
"my-app-high-cpu" alarm → check my-app-writer AND my-app-reader-1 simultaneously

## Redis Clusters
main-cache — primary app cache
session-cache — user sessions

## Key Services (namespace: default)
api-backend → my-app-writer + main-cache
worker → my-app-writer

## Known IAM Gaps (always fail — skip, don't retry)
aws rds describe-db-clusters → no permission

## Proven Commands
for i in my-app-writer my-app-reader-1; do
  aws cloudwatch get-metric-statistics --namespace AWS/RDS --metric-name CPUUtilization \
    --dimensions Name=DBInstanceIdentifier,Value=$i \
    --start-time START --end-time END --period 300 --statistics Average Maximum \
    --region us-east-1 --output json | jq -r '.Datapoints|sort_by(.Timestamp)[]|"\(.Timestamp): avg=\(.Average|floor)%"'
done
```

Update without rebuilding:
```bash
kubectl cp ./knowledge.md <namespace>/<pod>:/data/knowledge.md
kubectl rollout restart deployment/vishwakarma -n <namespace>
```

### 5. Deploy to Kubernetes

```bash
kubectl apply -f k8s/rbac.yaml        # ServiceAccount + ClusterRole
kubectl apply -f k8s/deployment.yaml  # PVC + ConfigMap + Deployment + Service
```

### 6. Alert Ingestion

**Option A: AlertManager webhook** (Prometheus / VMAlertManager)
```yaml
receivers:
  - name: vishwakarma
    webhook_configs:
      - url: http://vishwakarma.monitoring.svc.cluster.local:5050/api/alertmanager
```

**Option B: CloudWatch → Amazon Q → Slack**

Add the bot to the channel where Amazon Q posts CloudWatch alarms. It auto-detects the alarm format and forwards to the agentic loop.

**Option C: CloudWatch → SNS → Lambda → Vishwakarma**

Deploy the Lambda in `lambda/` — converts SNS CloudWatch events to AlertManager format and POSTs to `/api/alertmanager`.

---

## Directory Structure

```
vishwakarma/
├── core/
│   ├── engine.py          # Agentic loop — tool calling, parallelism, safeguards, checkpointing
│   ├── prompt.py          # System prompt builder (composable sections)
│   ├── tools.py           # Tool definitions + executor
│   ├── toolset_manager.py # Loads, validates, and manages toolsets
│   └── models.py          # Pydantic data models
├── plugins/
│   ├── toolsets/          # bash, prometheus, elasticsearch, grafana, aws, ...
│   ├── runbooks/          # Investigation runbooks (.md) — one per alert type
│   │   ├── aws/           # RDS, ALB, Redis runbooks
│   │   └── custom/        # Your cluster-specific runbooks
│   ├── agents/
│   │   └── agents.json    # Alert → runbook routing catalog
│   ├── channels/
│   │   └── alertmanager/  # AlertManager webhook parser
│   └── relays/
│       └── slack/         # Slack result poster (PDF + thread)
├── bot/
│   ├── slack.py           # Slack Socket Mode bot (@mention handler, CloudWatch detection)
│   ├── cloudwatch.py      # CloudWatch alarm parser (Amazon Q + SNS formats)
│   └── pdf.py             # PDF RCA report generation
├── storage/
│   └── db.py              # SQLite incident storage
├── server.py              # FastAPI server + pre-enrichment + alert routing
└── config.py              # Config loader (YAML + env vars)

k8s/
├── deployment.yaml        # PVC + ConfigMap + Deployment + Service
└── rbac.yaml              # ServiceAccount + ClusterRole + ClusterRoleBinding

lambda/
└── handler.py             # CloudWatch SNS → AlertManager forwarder

knowledge.md               # Gitignored — your site-specific knowledge base
```

---

## API

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/alertmanager` | POST | AlertManager/CloudWatch webhook — triggers investigation |
| `/api/investigate` | POST | Ad-hoc investigation (sync, waits for result) |
| `/api/investigate/stream` | POST | Ad-hoc investigation (SSE streaming) |
| `/api/incidents` | GET | List past investigations |
| `/api/incidents/{id}` | GET | Get single investigation with full tool output |
| `/api/stats` | GET | Investigation statistics |
| `/api/toolsets` | GET | List toolset health and enabled status |
| `/healthz` | GET | Liveness probe |
| `/readyz` | GET | Readiness probe |

---

## Adapting for Your Cluster

| What to change | Where |
|----------------|-------|
| LLM provider + model | `config.yaml` → `llm.model`, `llm.api_base` |
| Fast model (summarisation, extraction) | `config.yaml` → `llm.fast_model` |
| Cluster name shown to LLM | `config.yaml` → `cluster_name` |
| Prometheus / ES / Grafana URLs | `config.yaml` → `toolsets.*` |
| Which tools are available | `config.yaml` → `toolsets.*.enabled` |
| Bash allowlist (which CLIs are allowed) | `config.yaml` → `toolsets.bash.config.allow` |
| Investigation workflow for your alerts | `plugins/runbooks/<your-category>/` |
| Alert → runbook routing | `plugins/agents/agents.json` |
| Infra-specific context (instance names, endpoints, mappings) | `/data/knowledge.md` on PVC |
| Slack bot identity + tone | `bot/slack.py` → `_simple_chat` system prompt |
