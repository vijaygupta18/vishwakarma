"""
Kafka toolset — consumer group lag, topic metadata, partition status.

Config:
  bootstrap_servers: kafka:9092
  security_protocol: PLAINTEXT  # or SASL_SSL
  sasl_mechanism: PLAIN
  sasl_username: user
  sasl_password: pass
"""
import json
import logging
from typing import Any

from vishwakarma.core.tools import Toolset, ToolDef, ToolOutput, ToolStatus
from vishwakarma.core.toolset_manager import register_toolset

log = logging.getLogger(__name__)


@register_toolset
class KafkaToolset(Toolset):
    name = "kafka"
    description = "Inspect Kafka consumer group lag, topic partition offsets, and cluster health"

    def __init__(self, config: dict):
        self._config = config
        self._admin = None

    def _get_admin(self):
        if self._admin is not None:
            return self._admin
        from kafka import KafkaAdminClient
        kwargs: dict[str, Any] = {
            "bootstrap_servers": self._config.get("bootstrap_servers", "kafka:9092"),
            "request_timeout_ms": 15000,
        }
        security = self._config.get("security_protocol", "PLAINTEXT")
        if security != "PLAINTEXT":
            kwargs["security_protocol"] = security
            if self._config.get("sasl_mechanism"):
                kwargs["sasl_mechanism"] = self._config["sasl_mechanism"]
                kwargs["sasl_plain_username"] = self._config.get("sasl_username", "")
                kwargs["sasl_plain_password"] = self._config.get("sasl_password", "")
        self._admin = KafkaAdminClient(**kwargs)
        return self._admin

    def check_prerequisites(self) -> tuple[bool, str]:
        try:
            import kafka  # noqa
        except ImportError:
            return False, "kafka-python not installed (pip install kafka-python)"
        try:
            admin = self._get_admin()
            admin.list_topics()
            return True, ""
        except Exception as e:
            return False, f"Cannot connect to Kafka: {e}"

    def get_tools(self) -> list[ToolDef]:
        return [
            ToolDef(
                name="kafka_consumer_lag",
                description=(
                    "Get consumer group lag per partition. "
                    "Shows how far behind each consumer is from the latest offset."
                ),
                parameters={
                    "type": "object",
                    "properties": {
                        "group_id": {"type": "string", "description": "Consumer group ID"},
                        "topic": {
                            "type": "string",
                            "description": "Topic name to filter. Omit for all topics.",
                        },
                    },
                    "required": ["group_id"],
                },
            ),
            ToolDef(
                name="kafka_list_consumer_groups",
                description="List all consumer groups and their states.",
                parameters={"type": "object", "properties": {}, "required": []},
            ),
            ToolDef(
                name="kafka_list_topics",
                description="List all Kafka topics.",
                parameters={
                    "type": "object",
                    "properties": {
                        "filter": {"type": "string", "description": "Filter topics by prefix"},
                    },
                    "required": [],
                },
            ),
            ToolDef(
                name="kafka_describe_topic",
                description="Get partition count, replication factor, and configs for a topic.",
                parameters={
                    "type": "object",
                    "properties": {
                        "topic": {"type": "string"},
                    },
                    "required": ["topic"],
                },
            ),
        ]

    def execute(self, tool_name: str, params: dict) -> ToolOutput:
        dispatch = {
            "kafka_consumer_lag": self._consumer_lag,
            "kafka_list_consumer_groups": self._list_groups,
            "kafka_list_topics": self._list_topics,
            "kafka_describe_topic": self._describe_topic,
        }
        fn = dispatch.get(tool_name)
        if fn is None:
            return ToolOutput(status=ToolStatus.ERROR, error=f"Unknown tool: {tool_name}")
        return fn(params)

    def _consumer_lag(self, params: dict) -> ToolOutput:
        group_id = params["group_id"]
        filter_topic = params.get("topic")
        invocation = f"kafka_consumer_lag({group_id})"
        try:
            from kafka import KafkaConsumer, TopicPartition

            admin = self._get_admin()
            offsets = admin.list_consumer_group_offsets(group_id)

            bootstrap = self._config.get("bootstrap_servers", "kafka:9092")
            temp_consumer = KafkaConsumer(
                bootstrap_servers=bootstrap,
                enable_auto_commit=False,
            )

            lines = [f"Consumer group: {group_id}"]
            total_lag = 0
            for tp, committed in sorted(offsets.items(), key=lambda x: (x[0].topic, x[0].partition)):
                if filter_topic and tp.topic != filter_topic:
                    continue
                end_offsets = temp_consumer.end_offsets([tp])
                end = end_offsets.get(tp, 0)
                committed_offset = committed.offset if committed else 0
                lag = max(0, end - committed_offset)
                total_lag += lag
                lines.append(f"  {tp.topic}[{tp.partition}]: committed={committed_offset} end={end} lag={lag}")

            temp_consumer.close()
            lines.append(f"\nTotal lag: {total_lag}")

            if not offsets:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)

            return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(lines), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _list_groups(self, params: dict) -> ToolOutput:
        invocation = "kafka_list_consumer_groups()"
        try:
            admin = self._get_admin()
            groups = admin.list_consumer_groups()
            if not groups:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            lines = [f"{g[0]} ({g[1]})" for g in sorted(groups)]
            return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(lines), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _list_topics(self, params: dict) -> ToolOutput:
        prefix = params.get("filter", "")
        invocation = "kafka_list_topics()"
        try:
            admin = self._get_admin()
            topics = sorted(admin.list_topics())
            if prefix:
                topics = [t for t in topics if t.startswith(prefix)]
            if not topics:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(topics), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)

    def _describe_topic(self, params: dict) -> ToolOutput:
        topic = params["topic"]
        invocation = f"kafka_describe_topic({topic})"
        try:
            admin = self._get_admin()
            metadata = admin.describe_topics([topic])
            if not metadata:
                return ToolOutput(status=ToolStatus.NO_DATA, invocation=invocation)
            t = metadata[0]
            lines = [f"Topic: {t['topic']}"]
            for p in t.get("partitions", []):
                lines.append(
                    f"  Partition {p['partition']}: leader={p['leader']} "
                    f"replicas={p['replicas']} isr={p['isr']}"
                )
            return ToolOutput(status=ToolStatus.SUCCESS, output="\n".join(lines), invocation=invocation)
        except Exception as e:
            return ToolOutput(status=ToolStatus.ERROR, error=str(e), invocation=invocation)
