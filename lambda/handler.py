"""
AWS Lambda — CloudWatch alarm SNS → Vishwakarma webhook.

Deploy this Lambda with an SNS trigger on your CloudWatch alarms topic.
It converts the SNS/CloudWatch format to AlertManager format and forwards
to the Vishwakarma webhook endpoint.

Environment variables:
  VISHWAKARMA_URL: https://vishwakarma.monitoring.svc.cluster.local:5050
                   (or the ALB/ingress URL if calling from outside cluster)
  VISHWAKARMA_TOKEN: optional bearer token for auth
"""
import json
import logging
import os
import urllib.request
import urllib.error

log = logging.getLogger()
log.setLevel(logging.INFO)

VISHWAKARMA_URL = os.environ.get("VISHWAKARMA_URL", "").rstrip("/")
VISHWAKARMA_TOKEN = os.environ.get("VISHWAKARMA_TOKEN", "")


def handler(event, context):
    if not VISHWAKARMA_URL:
        log.error("VISHWAKARMA_URL not set")
        return {"statusCode": 500, "body": "VISHWAKARMA_URL not configured"}

    from vishwakarma.bot.cloudwatch import sns_to_alertmanager

    payload = sns_to_alertmanager(event)
    if not payload:
        log.info("Event is not a CloudWatch alarm, skipping")
        return {"statusCode": 200, "body": "not an alarm"}

    # Only forward ALARM state (not OK/resolved)
    alerts = [a for a in payload.get("alerts", []) if a.get("status") == "firing"]
    if not alerts:
        log.info("No firing alerts, skipping")
        return {"statusCode": 200, "body": "no firing alerts"}

    payload["alerts"] = alerts

    # Forward to Vishwakarma
    webhook_url = f"{VISHWAKARMA_URL}/api/alertmanager"
    body = json.dumps(payload).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if VISHWAKARMA_TOKEN:
        headers["Authorization"] = f"Bearer {VISHWAKARMA_TOKEN}"

    req = urllib.request.Request(webhook_url, data=body, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            resp_body = resp.read().decode("utf-8")
            log.info(f"Vishwakarma responded: {resp.status} {resp_body}")
            return {"statusCode": resp.status, "body": resp_body}
    except urllib.error.HTTPError as e:
        error_body = e.read().decode("utf-8")
        log.error(f"Vishwakarma HTTP error {e.code}: {error_body}")
        return {"statusCode": e.code, "body": error_body}
    except Exception as e:
        log.error(f"Failed to forward to Vishwakarma: {e}")
        raise
