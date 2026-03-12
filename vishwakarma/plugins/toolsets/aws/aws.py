"""
AWS toolset — query RDS, CloudWatch, ElastiCache, and EC2.

Authentication (in order of precedence):
  1. IRSA (IAM Roles for Service Accounts) — auto-used inside EKS pods
  2. Environment variables: AWS_ACCESS_KEY_ID + AWS_SECRET_ACCESS_KEY
  3. ~/.aws/credentials profile

Config options:
  region: <region>   (default: AWS_DEFAULT_REGION env or us-east-1)

Tools:
  aws_rds_describe_instances        — list RDS instances with status/metrics
  aws_rds_describe_events           — recent RDS events (maintenance, failover, etc.)
  aws_rds_get_performance_insights  — top SQL queries by load (requires PI enabled)
  aws_cloudwatch_get_metric         — get CloudWatch metric statistics
  aws_cloudwatch_list_alarms        — list alarms in ALARM state
  aws_cloudwatch_get_logs           — fetch CloudWatch Logs (RDS slow query, app logs)
  aws_elasticache_describe_clusters — list ElastiCache clusters with status
  aws_elasticache_describe_events   — recent ElastiCache events
  aws_ec2_describe_instances        — list EC2 instances (filter by tag/id)
"""
import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class AWSToolset(Toolset):
    name = "aws"
    description = (
        "Query AWS services — RDS (instances, events, Performance Insights), "
        "CloudWatch (metrics, alarms, logs), ElastiCache (clusters, events), EC2"
    )

    def __init__(self, config: dict):
        self._region = (
            config.get("region")
            or os.environ.get("AWS_DEFAULT_REGION")
            or os.environ.get("AWS_REGION")
            or "<region>"
        )
        self._config = config
        self._clients: dict[str, Any] = {}

    def _client(self, service: str):
        if service not in self._clients:
            import boto3
            kwargs: dict[str, Any] = {"region_name": self._region}
            if self._config.get("access_key_id"):
                kwargs["aws_access_key_id"] = self._config["access_key_id"]
                kwargs["aws_secret_access_key"] = self._config.get("secret_access_key", "")
            self._clients[service] = boto3.client(service, **kwargs)
        return self._clients[service]

    def check_prerequisites(self) -> tuple[bool, str]:
        try:
            import boto3  # noqa: F401
        except ImportError:
            return False, "boto3 not installed (pip install boto3)"
        try:
            sts = self._client("sts")
            identity = sts.get_caller_identity()
            account = identity.get("Account", "?")
            arn = identity.get("Arn", "?")
            return True, f"AWS authenticated as {arn} (account {account})"
        except Exception as e:
            return False, f"AWS auth failed: {e}"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="aws_rds_describe_instances",
                description=(
                    "List RDS DB instances. Returns instance ID, class, engine, status, "
                    "endpoint, Multi-AZ, storage, and current CloudWatch CPU/connections/memory. "
                    "Use to confirm which instance is affected and get its current state."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "db_instance_identifier": {
                            "type": "string",
                            "description": "Specific DB instance ID to fetch. Omit to list all.",
                        },
                        "region": {
                            "type": "string",
                            "description": "AWS region. Defaults to configured region.",
                        },
                    },
                    "required": [],
                },
            ),
            ToolDef(
                name="aws_rds_describe_events",
                description=(
                    "Get recent RDS events for a DB instance — maintenance, failover, "
                    "backup, parameter group changes, storage full, etc. "
                    "Use to detect unexpected changes or AWS-side issues."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "db_instance_identifier": {
                            "type": "string",
                            "description": "DB instance ID e.g. 'atlas-customer-v1-r1'",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "How many hours back to look. Default: 2",
                            "default": 2,
                        },
                        "region": {"type": "string"},
                    },
                    "required": ["db_instance_identifier"],
                },
            ),
            ToolDef(
                name="aws_rds_get_performance_insights",
                description=(
                    "Get top SQL queries by DB load from RDS Performance Insights. "
                    "Requires Performance Insights to be enabled on the instance. "
                    "Use to identify slow/expensive queries causing high CPU or IOPS."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "db_instance_identifier": {
                            "type": "string",
                            "description": "DB instance ID e.g. 'atlas-customer-v1-r1'",
                        },
                        "minutes": {
                            "type": "integer",
                            "description": "Window in minutes. Default: 30",
                            "default": 30,
                        },
                        "region": {"type": "string"},
                    },
                    "required": ["db_instance_identifier"],
                },
            ),
            ToolDef(
                name="aws_cloudwatch_get_metric",
                description=(
                    "Get CloudWatch metric statistics (Average, Maximum, Sum) over a time window. "
                    "Use for RDS CPU/connections/IOPS, ElastiCache memory/evictions, ALB request counts, etc."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "namespace": {
                            "type": "string",
                            "description": "Metric namespace e.g. 'AWS/RDS', 'AWS/ElastiCache', 'AWS/ApplicationELB'",
                        },
                        "metric_name": {
                            "type": "string",
                            "description": "Metric name e.g. 'CPUUtilization', 'DatabaseConnections', 'FreeableMemory'",
                        },
                        "dimensions": {
                            "type": "array",
                            "description": "Dimension filters e.g. [{\"Name\": \"DBInstanceIdentifier\", \"Value\": \"atlas-customer-v1-r1\"}]",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "Name": {"type": "string"},
                                    "Value": {"type": "string"},
                                },
                            },
                        },
                        "period": {
                            "type": "integer",
                            "description": "Granularity in seconds. Default: 60",
                            "default": 60,
                        },
                        "minutes": {
                            "type": "integer",
                            "description": "How many minutes back to fetch. Default: 60",
                            "default": 60,
                        },
                        "stat": {
                            "type": "string",
                            "description": "Statistic: Average, Maximum, Minimum, Sum, SampleCount. Default: Average",
                            "default": "Average",
                        },
                        "region": {"type": "string"},
                    },
                    "required": ["namespace", "metric_name"],
                },
            ),
            ToolDef(
                name="aws_cloudwatch_list_alarms",
                description=(
                    "List CloudWatch alarms currently in ALARM state (or all alarms). "
                    "Use to see what other alarms are firing alongside the incident."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "state": {
                            "type": "string",
                            "description": "Filter by state: ALARM, OK, INSUFFICIENT_DATA. Default: ALARM",
                            "enum": ["ALARM", "OK", "INSUFFICIENT_DATA"],
                            "default": "ALARM",
                        },
                        "alarm_name_prefix": {
                            "type": "string",
                            "description": "Filter alarms by name prefix e.g. 'atlas-customer'",
                        },
                        "region": {"type": "string"},
                    },
                    "required": [],
                },
            ),
            ToolDef(
                name="aws_cloudwatch_get_logs",
                description=(
                    "Fetch log events from a CloudWatch Log Group. "
                    "Useful for RDS slow query logs, Lambda logs, ECS task logs."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "log_group_name": {
                            "type": "string",
                            "description": "Log group name e.g. '/aws/rds/instance/atlas-customer-v1-r1/slowquery'",
                        },
                        "log_stream_name": {
                            "type": "string",
                            "description": "Specific log stream. Omit to search latest stream.",
                        },
                        "filter_pattern": {
                            "type": "string",
                            "description": "CloudWatch filter pattern e.g. 'ERROR', '{ $.level = \"error\" }'",
                        },
                        "minutes": {
                            "type": "integer",
                            "description": "How many minutes back to look. Default: 30",
                            "default": 30,
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Max log events to return. Default: 50",
                            "default": 50,
                        },
                        "region": {"type": "string"},
                    },
                    "required": ["log_group_name"],
                },
            ),
            ToolDef(
                name="aws_elasticache_describe_clusters",
                description=(
                    "List ElastiCache clusters (Redis/Memcached) with their status, node type, "
                    "engine version, and endpoint. Use to check cluster health during Redis alerts."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "cluster_id": {
                            "type": "string",
                            "description": "Specific cluster ID e.g. 'beckn-redis-cluster'. Omit to list all.",
                        },
                        "region": {"type": "string"},
                    },
                    "required": [],
                },
            ),
            ToolDef(
                name="aws_elasticache_describe_events",
                description=(
                    "Get recent ElastiCache events — node replacements, failovers, parameter changes."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "source_identifier": {
                            "type": "string",
                            "description": "Cluster or replication group ID. Omit for all events.",
                        },
                        "hours": {
                            "type": "integer",
                            "description": "How many hours back. Default: 2",
                            "default": 2,
                        },
                        "region": {"type": "string"},
                    },
                    "required": [],
                },
            ),
            ToolDef(
                name="aws_ec2_describe_instances",
                description=(
                    "List EC2 instances with their state, type, private IP, and tags. "
                    "Use to check if underlying EC2 instances are healthy."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "instance_ids": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Specific instance IDs. Omit to list all running instances.",
                        },
                        "filters": {
                            "type": "array",
                            "description": "EC2 filter list e.g. [{\"Name\": \"tag:Name\", \"Values\": [\"atlas-*\"]}]",
                            "items": {"type": "object"},
                        },
                        "region": {"type": "string"},
                    },
                    "required": [],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        # Allow per-call region override
        region = params.pop("region", None)
        if region:
            self._region = region
            self._clients = {}  # reset clients for new region

        dispatch = {
            "aws_rds_describe_instances": self._rds_describe_instances,
            "aws_rds_describe_events": self._rds_describe_events,
            "aws_rds_get_performance_insights": self._rds_performance_insights,
            "aws_cloudwatch_get_metric": self._cloudwatch_get_metric,
            "aws_cloudwatch_list_alarms": self._cloudwatch_list_alarms,
            "aws_cloudwatch_get_logs": self._cloudwatch_get_logs,
            "aws_elasticache_describe_clusters": self._elasticache_describe_clusters,
            "aws_elasticache_describe_events": self._elasticache_describe_events,
            "aws_ec2_describe_instances": self._ec2_describe_instances,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        try:
            return fn(params)
        except Exception as e:
            log.error(f"AWS tool {tool_name} failed: {e}", exc_info=True)
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=tool_name)

    # ── RDS ────────────────────────────────────────────────────────────────────

    def _rds_describe_instances(self, params: dict) -> ToolOutput:
        rds = self._client("rds")
        db_id = params.get("db_instance_identifier")
        invocation = f"aws_rds_describe_instances({db_id or 'all'})"

        kwargs: dict[str, Any] = {}
        if db_id:
            kwargs["DBInstanceIdentifier"] = db_id

        resp = rds.describe_db_instances(**kwargs)
        instances = resp.get("DBInstances", [])
        if not instances:
            return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)

        lines = []
        for db in instances:
            iid = db["DBInstanceIdentifier"]
            status = db["DBInstanceStatus"]
            cls = db["DBInstanceClass"]
            engine = f"{db['Engine']} {db.get('EngineVersion', '')}"
            multi_az = db.get("MultiAZ", False)
            storage = f"{db.get('AllocatedStorage', '?')}GB {db.get('StorageType', '')}"
            endpoint = db.get("Endpoint", {})
            host = f"{endpoint.get('Address', 'N/A')}:{endpoint.get('Port', '')}"
            lines.append(
                f"  ID: {iid}\n"
                f"  Status: {status} | Class: {cls} | Engine: {engine}\n"
                f"  MultiAZ: {multi_az} | Storage: {storage}\n"
                f"  Endpoint: {host}"
            )

        return ToolOutput(
            status=ToolStatus.SUCCESS,
            output=f"RDS Instances ({len(instances)}):\n\n" + "\n\n".join(lines),
            invocation=invocation,
        )

    def _rds_describe_events(self, params: dict) -> ToolOutput:
        rds = self._client("rds")
        db_id = params["db_instance_identifier"]
        hours = int(params.get("hours", 2))
        invocation = f"aws_rds_describe_events({db_id}, last {hours}h)"

        start_time = datetime.now(timezone.utc) - timedelta(hours=hours)
        resp = rds.describe_events(
            SourceIdentifier=db_id,
            SourceType="db-instance",
            StartTime=start_time,
        )
        events = resp.get("Events", [])
        if not events:
            return ToolOutput(
                status=ToolStatus.NO_DATA,
                output=f"No RDS events for {db_id} in the last {hours}h",
                invocation=invocation,
            )

        lines = []
        for e in events:
            ts = e.get("Date", "").isoformat() if hasattr(e.get("Date", ""), "isoformat") else str(e.get("Date", ""))
            msg = e.get("Message", "")
            categories = ", ".join(e.get("EventCategories", []))
            lines.append(f"[{ts}] [{categories}] {msg}")

        return ToolOutput(
            status=ToolStatus.SUCCESS,
            output="\n".join(lines),
            invocation=invocation,
        )

    def _rds_performance_insights(self, params: dict) -> ToolOutput:
        db_id = params["db_instance_identifier"]
        minutes = int(params.get("minutes", 30))
        invocation = f"aws_rds_get_performance_insights({db_id}, last {minutes}m)"

        # First get the DBI resource ID (needed for PI API)
        rds = self._client("rds")
        try:
            resp = rds.describe_db_instances(DBInstanceIdentifier=db_id)
            instances = resp.get("DBInstances", [])
            if not instances:
                return ToolOutput(status=ToolStatus.ERROR, error=f"Instance {db_id} not found", invocation=invocation)
            dbi_resource_id = instances[0].get("DbiResourceId")
            if not dbi_resource_id:
                return ToolOutput(status=ToolStatus.ERROR, error="Could not get DBI resource ID", invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Failed to get instance details: {e}", invocation=invocation)

        pi = self._client("pi")
        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=minutes)

        try:
            resp = pi.describe_dimension_keys(
                ServiceType="RDS",
                Identifier=dbi_resource_id,
                StartTime=start_time,
                EndTime=end_time,
                Metric="db.load.avg",
                GroupBy={"Group": "db.sql", "Limit": 10},
                PeriodInSeconds=60,
            )
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Performance Insights error: {e}", invocation=invocation)

        keys = resp.get("Keys", [])
        if not keys:
            return ToolOutput(
                status=ToolStatus.NO_DATA,
                output=f"No Performance Insights data for {db_id} (may not be enabled)",
                invocation=invocation,
            )

        lines = [f"Top SQL by DB Load (last {minutes}m) for {db_id}:"]
        for i, key in enumerate(keys, 1):
            dims = key.get("Dimensions", {})
            sql = dims.get("db.sql.statement", dims.get("db.sql.id", "unknown"))
            load = key.get("Total", 0)
            # Truncate long SQL
            sql_short = sql[:200].replace("\n", " ") if sql else "(no SQL)"
            lines.append(f"\n{i}. Load: {load:.3f}\n   SQL: {sql_short}")

        return ToolOutput(
            status=ToolStatus.SUCCESS,
            output="\n".join(lines),
            invocation=invocation,
        )

    # ── CloudWatch ─────────────────────────────────────────────────────────────

    def _cloudwatch_get_metric(self, params: dict) -> ToolOutput:
        cw = self._client("cloudwatch")
        namespace = params["namespace"]
        metric_name = params["metric_name"]
        dimensions = params.get("dimensions", [])
        period = int(params.get("period", 60))
        minutes = int(params.get("minutes", 60))
        stat = params.get("stat", "Average")

        invocation = f"aws_cloudwatch_get_metric({namespace}/{metric_name})"

        end_time = datetime.now(timezone.utc)
        start_time = end_time - timedelta(minutes=minutes)

        resp = cw.get_metric_statistics(
            Namespace=namespace,
            MetricName=metric_name,
            Dimensions=dimensions,
            StartTime=start_time,
            EndTime=end_time,
            Period=period,
            Statistics=[stat],
        )

        datapoints = sorted(resp.get("Datapoints", []), key=lambda x: x["Timestamp"])
        invocation = f"aws_cloudwatch_get_metric({namespace}/{metric_name}, last {minutes}m)"

        if not datapoints:
            return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)

        unit = datapoints[0].get("Unit", "")
        values = [dp[stat] for dp in datapoints]
        timestamps = [dp["Timestamp"].strftime("%H:%M") for dp in datapoints]

        # Summary stats
        avg = sum(values) / len(values)
        max_val = max(values)
        min_val = min(values)
        latest = values[-1]
        latest_ts = timestamps[-1]

        # Sparkline (last 10 points)
        recent = values[-10:]
        if max_val > 0:
            bar_chars = "▁▂▃▄▅▆▇█"
            spark = "".join(bar_chars[min(int(v / max_val * 7), 7)] for v in recent)
        else:
            spark = "─" * len(recent)

        lines = [
            f"Metric: {namespace}/{metric_name} ({stat})",
            f"Window: last {minutes}m, period={period}s, {len(datapoints)} datapoints",
            f"Stats:  min={min_val:.2f} avg={avg:.2f} max={max_val:.2f} latest={latest:.2f} {unit}",
            f"Trend:  {spark} (latest @ {latest_ts})",
        ]

        # Show individual datapoints if few enough
        if len(datapoints) <= 20:
            lines.append("\nDatapoints:")
            for ts, v in zip(timestamps, values):
                lines.append(f"  {ts}  {v:.2f} {unit}")

        return ToolOutput(
            status=ToolStatus.SUCCESS,
            output="\n".join(lines),
            invocation=invocation,
        )

    def _cloudwatch_list_alarms(self, params: dict) -> ToolOutput:
        cw = self._client("cloudwatch")
        state = params.get("state", "ALARM")
        prefix = params.get("alarm_name_prefix", "")
        invocation = f"aws_cloudwatch_list_alarms(state={state})"

        kwargs: dict[str, Any] = {"StateValue": state}
        if prefix:
            kwargs["AlarmNamePrefix"] = prefix

        resp = cw.describe_alarms(**kwargs)
        alarms = resp.get("MetricAlarms", []) + resp.get("CompositeAlarms", [])

        if not alarms:
            return ToolOutput(
                status=ToolStatus.NO_DATA,
                output=f"No alarms in {state} state",
                invocation=invocation,
            )

        lines = [f"CloudWatch Alarms ({state}): {len(alarms)}"]
        for a in alarms:
            name = a.get("AlarmName", "?")
            reason = a.get("StateReason", "")[:150]
            updated = a.get("StateUpdatedTimestamp", "")
            if hasattr(updated, "strftime"):
                updated = updated.strftime("%Y-%m-%d %H:%M UTC")
            metric = f"{a.get('Namespace', '')}/{a.get('MetricName', '')}"
            lines.append(f"\n[{state}] {name}\n  Metric: {metric}\n  Since: {updated}\n  Reason: {reason}")

        return ToolOutput(
            status=ToolStatus.SUCCESS,
            output="\n".join(lines),
            invocation=invocation,
        )

    def _cloudwatch_get_logs(self, params: dict) -> ToolOutput:
        logs_client = self._client("logs")
        log_group = params["log_group_name"]
        stream_name = params.get("log_stream_name")
        filter_pattern = params.get("filter_pattern", "")
        minutes = int(params.get("minutes", 30))
        limit = int(params.get("limit", 50))
        invocation = f"aws_cloudwatch_get_logs({log_group}, last {minutes}m)"

        end_ms = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_ms = end_ms - (minutes * 60 * 1000)

        if stream_name:
            # Get specific stream
            kwargs: dict[str, Any] = {
                "logGroupName": log_group,
                "logStreamName": stream_name,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            }
            resp = logs_client.get_log_events(**kwargs)
            events = resp.get("events", [])
        else:
            # Filter across all streams
            kwargs = {
                "logGroupName": log_group,
                "startTime": start_ms,
                "endTime": end_ms,
                "limit": limit,
            }
            if filter_pattern:
                kwargs["filterPattern"] = filter_pattern
            resp = logs_client.filter_log_events(**kwargs)
            events = resp.get("events", [])

        if not events:
            return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)

        lines = [f"Log Group: {log_group} (last {minutes}m, {len(events)} events)"]
        for e in events:
            ts_ms = e.get("timestamp", 0)
            ts = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc).strftime("%H:%M:%S")
            msg = e.get("message", "").rstrip()
            lines.append(f"[{ts}] {msg}")

        return ToolOutput(
            status=ToolStatus.SUCCESS,
            output="\n".join(lines),
            invocation=invocation,
        )

    # ── ElastiCache ────────────────────────────────────────────────────────────

    def _elasticache_describe_clusters(self, params: dict) -> ToolOutput:
        ec = self._client("elasticache")
        cluster_id = params.get("cluster_id")
        invocation = f"aws_elasticache_describe_clusters({cluster_id or 'all'})"

        kwargs: dict[str, Any] = {"ShowCacheClustersNotInReplicationGroups": False}
        if cluster_id:
            kwargs["CacheClusterId"] = cluster_id

        # Try replication groups first (Redis clusters are usually RGs)
        rgs = ec.describe_replication_groups().get("ReplicationGroups", [])
        clusters = ec.describe_cache_clusters(ShowCacheNodeInfo=True).get("CacheClusters", [])

        if not rgs and not clusters:
            return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)

        lines = []
        if rgs:
            lines.append(f"Replication Groups ({len(rgs)}):")
            for rg in rgs:
                rgid = rg["ReplicationGroupId"]
                status = rg["Status"]
                desc = rg.get("Description", "")
                node_groups = rg.get("NodeGroups", [])
                primary = ""
                for ng in node_groups:
                    for member in ng.get("NodeGroupMembers", []):
                        if member.get("CurrentRole") == "primary":
                            primary = member.get("ReadEndpoint", {}).get("Address", "")
                lines.append(f"  {rgid}: {status} | {desc} | primary: {primary}")

        if clusters:
            lines.append(f"\nCache Clusters ({len(clusters)}):")
            for c in clusters:
                cid = c["CacheClusterId"]
                status = c["CacheClusterStatus"]
                engine = f"{c.get('Engine', '')} {c.get('EngineVersion', '')}"
                node_type = c.get("CacheNodeType", "")
                lines.append(f"  {cid}: {status} | {engine} | {node_type}")

        return ToolOutput(
            status=ToolStatus.SUCCESS,
            output="\n".join(lines),
            invocation=invocation,
        )

    def _elasticache_describe_events(self, params: dict) -> ToolOutput:
        ec = self._client("elasticache")
        source_id = params.get("source_identifier")
        hours = int(params.get("hours", 2))
        invocation = f"aws_elasticache_describe_events({source_id or 'all'}, last {hours}h)"

        start_time = datetime.now(timezone.utc) - timedelta(hours=hours)
        kwargs: dict[str, Any] = {"StartTime": start_time, "MaxRecords": 50}
        if source_id:
            kwargs["SourceIdentifier"] = source_id

        resp = ec.describe_events(**kwargs)
        events = resp.get("Events", [])
        if not events:
            return ToolOutput(
                status=ToolStatus.NO_DATA,
                output=f"No ElastiCache events in the last {hours}h",
                invocation=invocation,
            )

        lines = []
        for e in events:
            ts = e.get("Date", "")
            if hasattr(ts, "isoformat"):
                ts = ts.strftime("%Y-%m-%d %H:%M UTC")
            src = e.get("SourceIdentifier", "")
            msg = e.get("Message", "")
            lines.append(f"[{ts}] {src}: {msg}")

        return ToolOutput(
            status=ToolStatus.SUCCESS,
            output="\n".join(lines),
            invocation=invocation,
        )

    # ── EC2 ────────────────────────────────────────────────────────────────────

    def _ec2_describe_instances(self, params: dict) -> ToolOutput:
        ec2 = self._client("ec2")
        instance_ids = params.get("instance_ids", [])
        filters = params.get("filters", [{"Name": "instance-state-name", "Values": ["running"]}])
        invocation = f"aws_ec2_describe_instances({instance_ids or filters})"

        kwargs: dict[str, Any] = {}
        if instance_ids:
            kwargs["InstanceIds"] = instance_ids
        if filters and not instance_ids:
            kwargs["Filters"] = filters

        resp = ec2.describe_instances(**kwargs)
        reservations = resp.get("Reservations", [])
        instances = [i for r in reservations for i in r.get("Instances", [])]

        if not instances:
            return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)

        lines = [f"EC2 Instances ({len(instances)}):"]
        for inst in instances:
            iid = inst["InstanceId"]
            state = inst.get("State", {}).get("Name", "?")
            itype = inst.get("InstanceType", "?")
            private_ip = inst.get("PrivateIpAddress", "N/A")
            tags = {t["Key"]: t["Value"] for t in inst.get("Tags", [])}
            name = tags.get("Name", "")
            lines.append(f"  {iid} ({name}): {state} | {itype} | {private_ip}")

        return ToolOutput(
            status=ToolStatus.SUCCESS,
            output="\n".join(lines),
            invocation=invocation,
        )
