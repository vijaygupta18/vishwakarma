# Vishwakarma

Autonomous SRE investigation agent. Receives alerts, runs a multi-step agentic investigation using your observability stack (CloudWatch, Prometheus, Elasticsearch, Kubernetes), and posts a structured RCA to Slack with a PDF report.


---

## How It Works

```
Alert fires (CloudWatch / AlertManager)
  → POST /api/alertmanager
    → Dedup check (skip if same alert already being investigated)
      → Load matching runbook from agents.json
        → Load site knowledge base (/data/knowledge.md)
          → Agentic loop (up to 40 steps, parallel tool calls)
            → Slack: post RCA header + PDF in thread
              → Save to SQLite (/data/vishwakarma.db)
```

The agentic loop uses OpenAI function calling format. At each step the LLM decides which tools to call, results are fed back, and the loop continues until the LLM produces a final answer or hits `max_steps`.

---

## Features

- **Runbook routing** — per-alert runbooks matched by keyword, then LLM classification fallback
- **Site knowledge base** — `/data/knowledge.md` on PVC, injected into every investigation, no rebuild needed to update
- **Incident history** — SQLite DB stores all investigations; prior findings for recurring alerts are injected as context
- **Parallel tool execution** — up to 16 tools run simultaneously per step
- **PDF reports** — full RCA with evidence chain uploaded to Slack thread
- **Slack bot** — `@vishwakarma debug <question>` for on-demand investigations
- **Context compaction** — long investigations auto-compact to stay within LLM context window

---

## Setup

### 1. Prerequisites
- Kubernetes cluster with `kubectl` access
- LLM API (OpenAI-compatible — set `api_base` for self-hosted)
- Slack app with Bot Token (`xoxb-`) and App Token (`xapp-`) for Socket Mode
- AWS IRSA or env var credentials for CloudWatch/RDS queries (if using AWS)

### 2. Configure

Copy and fill in the example config:
```bash
cp config.example.yaml config.yaml   # gitignored
```

Key fields to change for your cluster:

```yaml
llm:
  model: openai/gpt-4o          # or openai/open-large for Juspay AI
  api_base: https://...          # omit for OpenAI default
  api_key: sk-...                # or set VK_API_KEY env var

cluster_name: my-cluster         # shown to LLM in every investigation

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
      allow: [kubectl, aws, stern]
      block: [rm, curl, wget]
```

### 3. Runbooks

Runbooks are `.md` files in `vishwakarma/plugins/runbooks/`. Each runbook is matched to alerts via `vishwakarma/plugins/agents/agents.json`.

**To add a runbook for your cluster:**

1. Create `vishwakarma/plugins/runbooks/<category>/<alert-name>.md` — describe the investigation workflow, exact commands for your infra, and expected findings.

2. Register it in `agents.json`:
```json
{
  "id": "my-alert-investigation",
  "description": "Investigate MyAlert — what it means and how to diagnose",
  "keywords": ["myalert", "keyword2"],
  "runbook": "../runbooks/<category>/<alert-name>.md"
}
```

The `keywords` list is matched against the alert name (case-insensitive). If no keyword matches, the LLM picks the best runbook from the catalog automatically.

### 4. Site Knowledge Base

Create `/data/knowledge.md` on your PVC with cluster-specific context that should be available in **every** investigation:

```markdown
## RDS Instances
my-app-writer (writer), my-app-reader (reader) — aurora-postgresql, region: us-east-1

## Alert → Instance Mapping
"my-app-high-cpu" alarm → check my-app-writer AND my-app-reader

## Known IAM Gaps
aws rds describe-db-clusters → no permission
```

This file is injected into the system prompt for every investigation. Update it without rebuilding:
```bash
kubectl cp ./knowledge.md <namespace>/<pod>:/data/knowledge.md
kubectl rollout restart deployment/vishwakarma -n <namespace>
```

### 5. Deploy to Kubernetes

```bash
# Copy and fill in the example deployment (gitignore your copy)
cp k8s/deployment.example.yaml k8s/deployment.yaml

# Fill in:
#   - image tag
#   - SLACK_BOT_TOKEN, SLACK_APP_TOKEN
#   - VK_API_KEY
#   - storageClassName for your cluster
#   - toolset URLs in the ConfigMap

kubectl apply -f k8s/rbac.yaml        # ServiceAccount + ClusterRole
kubectl apply -f k8s/deployment.yaml  # PVC + ConfigMap + Deployment + Service
```

### 6. Alert Ingestion

**Option A: AlertManager webhook** (Prometheus)
Point AlertManager at `http://vishwakarma.<namespace>.svc.cluster.local:5050/api/alertmanager`

```yaml
# alertmanager.yaml
receivers:
  - name: vishwakarma
    webhook_configs:
      - url: http://vishwakarma.monitoring.svc.cluster.local:5050/api/alertmanager
```

**Option B: CloudWatch → Slack → Vishwakarma** (AWS)
If Amazon Q posts CloudWatch alarms to a Slack channel, add the Vishwakarma bot to that channel. It auto-detects the alarm format and forwards to `/api/alertmanager`.

**Option C: CloudWatch → SNS → Lambda → Vishwakarma**
Deploy the Lambda in `lambda/` — it converts SNS CloudWatch events to AlertManager format and POSTs to the vishwakarma endpoint.

---

## Directory Structure

```
vishwakarma/
├── core/
│   ├── engine.py          # Agentic loop
│   ├── prompt.py          # System prompt builder
│   ├── tools.py           # Tool definitions + executor
│   ├── toolset_manager.py # Loads + manages toolsets
│   └── models.py          # Pydantic data models
├── plugins/
│   ├── toolsets/          # bash, prometheus, elasticsearch, grafana, http, ...
│   ├── runbooks/          # Investigation runbooks (.md)
│   │   ├── aws/           # RDS, ALB, Redis
│   │   └── sre-platform/    # App-specific runbooks (example — replace with yours)
│   ├── agents/
│   │   └── agents.json    # Runbook routing catalog
│   ├── channels/
│   │   └── alertmanager/  # AlertManager webhook parser
│   └── relays/
│       └── slack/         # Slack result poster (PDF + thread)
├── bot/
│   ├── slack.py           # Slack Socket Mode bot
│   └── cloudwatch.py      # CloudWatch alarm parser
├── storage/
│   └── db.py              # SQLite incident storage
├── server.py              # FastAPI server
└── config.py              # Config loader

k8s/
├── deployment.example.yaml   # Template — copy to deployment.yaml (gitignored)
└── rbac.example.yaml         # Template — copy to rbac.yaml (gitignored)

knowledge.md                  # Gitignored — your site-specific knowledge base
deployment.md                 # Gitignored — your deployment notes
```

---

## Adapting for Your Cluster

| What to change | Where |
|---|---|
| LLM provider + model | `config.yaml` → `llm.model`, `llm.api_base` |
| Cluster name shown to LLM | `config.yaml` → `cluster_name` |
| Prometheus / ES / Grafana URLs | `config.yaml` → `toolsets.*` |
| Which tools are available | `config.yaml` → `toolsets.*.enabled` |
| Investigation workflow for your alerts | `plugins/runbooks/<your-category>/` |
| Alert → runbook routing | `plugins/agents/agents.json` |
| Infra-specific context (instance names, mappings) | `/data/knowledge.md` on PVC |
| App-specific namespaces / service names | Update runbooks + knowledge.md |

The `sre-platform/` runbooks are example runbooks specific to that deployment. Replace or add your own — the structure is the same: describe the alert, list the exact commands to run, and describe how to interpret findings.

---

## API

| Endpoint | Description |
|---|---|
| `POST /api/alertmanager` | AlertManager/CloudWatch webhook |
| `POST /api/investigate` | Ad-hoc investigation (sync) |
| `POST /api/investigate/stream` | Ad-hoc investigation (SSE streaming) |
| `GET /api/incidents` | List past investigations |
| `GET /api/incidents/{id}` | Get single investigation |
| `GET /api/toolsets` | List toolset health |
| `GET /healthz` | Liveness probe |
| `GET /readyz` | Readiness probe |
