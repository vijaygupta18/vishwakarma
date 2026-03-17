"""
Daily AWS Cost Explorer report — fetches spend data, analyzes with LLM, posts PDF to Slack.

Runs on a daily timer (default 06:30 UTC / 12:00 IST). Started as a daemon thread
from cli.py:serve(), matching the existing Slack bot pattern.

Scheduled runs only post when anomalies are detected (cost spike above threshold).
On-demand runs (via @oogway costs) always post the full report.
"""
import logging
import threading
from datetime import datetime, timedelta, timezone

log = logging.getLogger(__name__)

# ── Concurrency guards ──────────────────────────────────────────────────────
_cost_report_lock = threading.Lock()
_investigation_running = threading.Event()  # set() = running, clear() = idle


def start_cost_reporter(config) -> None:
    """Start the daily cost report scheduler. Called from cli.py:serve()."""
    cr = config.cost_report
    if not cr["enabled"]:
        log.info("Cost reporter disabled — skipping")
        return

    if not config.slack_bot_token:
        log.warning("Cost reporter enabled but Slack not configured — skipping")
        return

    delay = _seconds_until(cr["schedule_utc"])
    log.info(
        f"Cost reporter scheduled — first run in {delay // 3600}h {(delay % 3600) // 60}m "
        f"(at {cr['schedule_utc']} UTC daily, channel: {cr['channel'] or 'default'})"
    )

    t = threading.Thread(target=_run_loop, args=(config,), daemon=True)
    t.start()


def _seconds_until(time_str: str) -> float:
    """Seconds from now until the next occurrence of HH:MM UTC."""
    h, m = map(int, time_str.split(":"))
    now = datetime.now(timezone.utc)
    target = now.replace(hour=h, minute=m, second=0, microsecond=0)
    if target <= now:
        target += timedelta(days=1)
    return (target - now).total_seconds()


def _run_loop(config):
    """Timer loop: wait → run report → reschedule. Never crashes."""
    delay = _seconds_until(config.cost_report["schedule_utc"])
    event = threading.Event()
    event.wait(timeout=delay)

    while True:
        try:
            log.info("Cost reporter: starting daily report")
            _generate_and_post(config, force=False)
        except Exception:
            log.exception("Cost reporter: failed to generate report")

        # Reschedule for next day
        delay = _seconds_until(config.cost_report["schedule_utc"])
        log.info(f"Cost reporter: next run in {delay // 3600:.0f}h {(delay % 3600) // 60:.0f}m")
        event.wait(timeout=delay)


def _fetch_cost_data(region: str = "ap-south-1") -> dict:
    """
    Fetch 30 days of cost data from AWS Cost Explorer, grouped by service.
    30 days gives a stable baseline and catches gradual cost climbs that
    a 7-day window would miss.

    Returns structured dict with daily totals, service breakdown, averages,
    and week-over-week comparison.
    """
    import boto3

    ce = boto3.client("ce", region_name=region)
    # CE end date is exclusive — use tomorrow to include today's (partial) data
    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    start = end - timedelta(days=31)

    all_results = []
    next_token = None
    while True:
        kwargs = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = ce.get_cost_and_usage(**kwargs)
        all_results.extend(resp["ResultsByTime"])
        next_token = resp.get("NextPageToken")
        if not next_token:
            break

    # Parse into structured data
    daily_totals = {}  # date -> total
    service_costs = {}  # service -> {date -> cost}

    for result in all_results:
        date = result["TimePeriod"]["Start"]
        day_total = 0.0
        for group in result["Groups"]:
            service = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost < 0.01:
                continue
            day_total += cost
            service_costs.setdefault(service, {})[date] = cost
        daily_totals[date] = round(day_total, 2)

    dates_sorted = sorted(daily_totals.keys())

    # Compute averages using days 3-23 (exclude last 2 days + first 5 for stable baseline)
    avg_dates = dates_sorted[5:-2] if len(dates_sorted) > 7 else dates_sorted[:-2] if len(dates_sorted) > 2 else dates_sorted
    baseline_avg = sum(daily_totals[d] for d in avg_dates) / max(len(avg_dates), 1)

    # Service-level averages
    service_avgs = {}
    for svc, date_costs in service_costs.items():
        avg_vals = [date_costs.get(d, 0) for d in avg_dates]
        service_avgs[svc] = sum(avg_vals) / max(len(avg_vals), 1)

    # Week-over-week: compare last 7 days total vs prior 7 days total
    last_7 = dates_sorted[-7:] if len(dates_sorted) >= 7 else dates_sorted
    prior_7 = dates_sorted[-14:-7] if len(dates_sorted) >= 14 else []
    last_7_total = sum(daily_totals[d] for d in last_7)
    prior_7_total = sum(daily_totals[d] for d in prior_7) if prior_7 else 0
    wow_pct = ((last_7_total - prior_7_total) / prior_7_total * 100) if prior_7_total > 0 else 0

    return {
        "daily_totals": daily_totals,
        "service_costs": service_costs,
        "service_avgs": service_avgs,
        "baseline_avg": round(baseline_avg, 2),
        "dates_sorted": dates_sorted,
        "last_7_total": round(last_7_total, 2),
        "prior_7_total": round(prior_7_total, 2),
        "wow_pct": round(wow_pct, 1),
    }


def _fetch_hourly_comparison(region: str = "ap-south-1") -> dict | None:
    """
    Fetch HOURLY cost data for today + yesterday to:
    1. Detect if today's data is complete or partial
    2. Compare today's hourly run rate vs yesterday's
    3. Project today's full-day cost
    4. Per-service: what increased and by how much

    Returns dict with today/yesterday hourly breakdown, projection, and per-service comparison.
    """
    import boto3

    ce = boto3.client("ce", region_name=region)
    today = datetime.now(timezone.utc).date()
    yesterday = today - timedelta(days=1)
    tomorrow = today + timedelta(days=1)

    all_results = []
    next_token = None
    while True:
        kwargs = {
            "TimePeriod": {
                "Start": f"{yesterday.isoformat()}T00:00:00Z",
                "End": f"{tomorrow.isoformat()}T00:00:00Z",
            },
            "Granularity": "HOURLY",
            "Metrics": ["UnblendedCost"],
            "GroupBy": [{"Type": "DIMENSION", "Key": "SERVICE"}],
        }
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = ce.get_cost_and_usage(**kwargs)
        all_results.extend(resp["ResultsByTime"])
        next_token = resp.get("NextPageToken")
        if not next_token:
            break

    # Parse hourly data into today vs yesterday
    today_str = today.isoformat()
    yesterday_str = yesterday.isoformat()

    today_total = 0.0
    yesterday_total = 0.0
    today_hours = 0
    yesterday_hours = 0
    today_svc = {}   # service -> cost
    yesterday_svc = {}

    for result in all_results:
        period_start = result["TimePeriod"]["Start"][:10]  # date part
        hour_total = 0.0
        for group in result["Groups"]:
            svc = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost < 0.001:
                continue
            hour_total += cost
            if period_start == today_str:
                today_svc[svc] = today_svc.get(svc, 0) + cost
            else:
                yesterday_svc[svc] = yesterday_svc.get(svc, 0) + cost

        if hour_total > 0.001:
            if period_start == today_str:
                today_total += hour_total
                today_hours += 1
            else:
                yesterday_total += hour_total
                yesterday_hours += 1

    if yesterday_hours == 0:
        return None

    today_complete = today_hours >= 22  # 22+ hours = effectively complete
    hourly_rate_today = today_total / max(today_hours, 1)
    hourly_rate_yesterday = yesterday_total / max(yesterday_hours, 1)

    # Guard: don't project if we have fewer than 4 hours of data — too unreliable
    insufficient_data = today_hours < 4 and not today_complete
    if insufficient_data:
        projected_today = None
    else:
        projected_today = hourly_rate_today * 24 if not today_complete else today_total

    # Per-service comparison: today's hourly rate vs yesterday's
    all_svcs = set(list(today_svc.keys()) + list(yesterday_svc.keys()))
    service_comparison = []
    for svc in all_svcs:
        t_cost = today_svc.get(svc, 0)
        y_cost = yesterday_svc.get(svc, 0)
        t_rate = t_cost / max(today_hours, 1)
        y_rate = y_cost / max(yesterday_hours, 1)
        if insufficient_data:
            projected_svc = None
            diff = None
            pct = None
        else:
            projected_svc = t_rate * 24 if not today_complete else t_cost
            diff = projected_svc - y_cost
            pct = ((projected_svc - y_cost) / y_cost * 100) if y_cost > 0 else (100 if projected_svc > 0 else 0)
        if t_cost >= 0.10 or y_cost >= 0.10:
            service_comparison.append({
                "service": svc,
                "today_so_far": round(t_cost, 2),
                "today_projected": round(projected_svc, 2) if projected_svc is not None else None,
                "yesterday": round(y_cost, 2),
                "diff": round(diff, 2) if diff is not None else None,
                "pct_change": round(pct, 1) if pct is not None else None,
            })
    service_comparison.sort(key=lambda x: -abs(x["diff"] or 0))

    result = {
        "today_date": today_str,
        "yesterday_date": yesterday_str,
        "today_total_so_far": round(today_total, 2),
        "today_hours": today_hours,
        "today_complete": today_complete,
        "today_projected": round(projected_today, 2) if projected_today is not None else None,
        "yesterday_total": round(yesterday_total, 2),
        "yesterday_hours": yesterday_hours,
        "hourly_rate_today": round(hourly_rate_today, 2),
        "hourly_rate_yesterday": round(hourly_rate_yesterday, 2),
        "service_comparison": service_comparison[:15],
    }
    if insufficient_data:
        result["note"] = "insufficient data for projection"
    return result


def _fetch_usage_breakdown(service_name: str, region: str = "ap-south-1") -> list[dict]:
    """
    For an anomalous service, fetch USAGE_TYPE breakdown (last 2 days vs prior 7).
    Returns list of {usage_type, yesterday_cost, avg_cost, pct_change, dollar_change}
    sorted by dollar_change descending — top contributors to the spike.

    This is the key to answering WHY a service cost increased: was it a new instance,
    storage growth, data transfer, backups, IOPS, etc.
    """
    import boto3

    ce = boto3.client("ce", region_name=region)
    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    start = end - timedelta(days=10)

    all_results = []
    next_token = None
    while True:
        kwargs = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "Filter": {"Dimensions": {"Key": "SERVICE", "Values": [service_name]}},
            "GroupBy": [{"Type": "DIMENSION", "Key": "USAGE_TYPE"}],
        }
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = ce.get_cost_and_usage(**kwargs)
        all_results.extend(resp["ResultsByTime"])
        next_token = resp.get("NextPageToken")
        if not next_token:
            break

    # Parse: usage_type -> {date -> cost}
    usage_by_date = {}  # usage_type -> {date -> cost}
    dates = []
    for result in all_results:
        date = result["TimePeriod"]["Start"]
        dates.append(date)
        for group in result["Groups"]:
            utype = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost < 0.001:
                continue
            usage_by_date.setdefault(utype, {})[date] = cost

    dates_sorted = sorted(dates)
    if len(dates_sorted) < 3:
        return []

    latest = dates_sorted[-1]
    avg_dates = dates_sorted[:-2]

    breakdown = []
    for utype, date_costs in usage_by_date.items():
        yesterday_cost = date_costs.get(latest, 0)
        avg_vals = [date_costs.get(d, 0) for d in avg_dates]
        avg_cost = sum(avg_vals) / max(len(avg_vals), 1)
        dollar_change = yesterday_cost - avg_cost
        pct_change = ((yesterday_cost - avg_cost) / avg_cost * 100) if avg_cost > 0 else (100.0 if yesterday_cost > 0 else 0)

        # Only include if there's meaningful cost or change
        if yesterday_cost >= 0.01 or abs(dollar_change) >= 0.01:
            breakdown.append({
                "usage_type": utype,
                "yesterday_cost": round(yesterday_cost, 2),
                "avg_cost": round(avg_cost, 2),
                "pct_change": round(pct_change, 1),
                "dollar_change": round(dollar_change, 2),
            })

    # Sort by dollar change descending — biggest contributors first
    breakdown.sort(key=lambda x: -abs(x["dollar_change"]))
    return breakdown[:15]  # top 15 usage types


def _fetch_operation_breakdown(service_name: str, region: str = "ap-south-1") -> list[dict]:
    """
    For an anomalous service, fetch OPERATION breakdown (last 2 days vs prior 7).
    Operations reveal WHAT HAPPENED: CreateDBInstance (new), ModifyDBInstance (resize),
    RunInstances (new EC2), PutObject (S3 writes), NatGateway (traffic), etc.

    This is the layer that answers "what changed?" vs USAGE_TYPE which answers "what resource type?".
    """
    import boto3

    ce = boto3.client("ce", region_name=region)
    end = datetime.now(timezone.utc).date() + timedelta(days=1)
    start = end - timedelta(days=10)

    all_results = []
    next_token = None
    while True:
        kwargs = {
            "TimePeriod": {"Start": start.isoformat(), "End": end.isoformat()},
            "Granularity": "DAILY",
            "Metrics": ["UnblendedCost"],
            "Filter": {"Dimensions": {"Key": "SERVICE", "Values": [service_name]}},
            "GroupBy": [{"Type": "DIMENSION", "Key": "OPERATION"}],
        }
        if next_token:
            kwargs["NextPageToken"] = next_token
        resp = ce.get_cost_and_usage(**kwargs)
        all_results.extend(resp["ResultsByTime"])
        next_token = resp.get("NextPageToken")
        if not next_token:
            break

    ops_by_date = {}
    dates = []
    for result in all_results:
        date = result["TimePeriod"]["Start"]
        dates.append(date)
        for group in result["Groups"]:
            op = group["Keys"][0]
            cost = float(group["Metrics"]["UnblendedCost"]["Amount"])
            if cost < 0.001:
                continue
            ops_by_date.setdefault(op, {})[date] = cost

    dates_sorted = sorted(dates)
    if len(dates_sorted) < 3:
        return []

    latest = dates_sorted[-1]
    avg_dates = dates_sorted[:-2]

    breakdown = []
    for op, date_costs in ops_by_date.items():
        yesterday_cost = date_costs.get(latest, 0)
        avg_vals = [date_costs.get(d, 0) for d in avg_dates]
        avg_cost = sum(avg_vals) / max(len(avg_vals), 1)
        dollar_change = yesterday_cost - avg_cost
        pct_change = ((yesterday_cost - avg_cost) / avg_cost * 100) if avg_cost > 0 else (100.0 if yesterday_cost > 0 else 0)

        if yesterday_cost >= 0.01 or abs(dollar_change) >= 0.01:
            breakdown.append({
                "operation": op,
                "yesterday_cost": round(yesterday_cost, 2),
                "avg_cost": round(avg_cost, 2),
                "pct_change": round(pct_change, 1),
                "dollar_change": round(dollar_change, 2),
            })

    breakdown.sort(key=lambda x: -abs(x["dollar_change"]))
    return breakdown[:10]


def _fetch_cost_forecast(region: str = "ap-south-1") -> dict | None:
    """
    Get AWS cost forecast for the rest of the current month.
    Returns {forecast_total, forecast_period, mean_daily} or None on failure.
    """
    import boto3

    ce = boto3.client("ce", region_name=region)
    today = datetime.now(timezone.utc).date()

    # Forecast from tomorrow to end of month
    start = today + timedelta(days=1)
    # End of current month
    if today.month == 12:
        end = today.replace(year=today.year + 1, month=1, day=1)
    else:
        end = today.replace(month=today.month + 1, day=1)

    # If we're at end of month, not enough days to forecast
    if start >= end:
        return None

    try:
        resp = ce.get_cost_forecast(
            TimePeriod={"Start": start.isoformat(), "End": end.isoformat()},
            Metric="UNBLENDED_COST",
            Granularity="MONTHLY",
        )
        total = float(resp["Total"]["Amount"])
        days_remaining = (end - start).days
        return {
            "forecast_remaining": round(total, 2),
            "days_remaining": days_remaining,
            "mean_daily_forecast": round(total / max(days_remaining, 1), 2),
            "period": f"{start.isoformat()} to {end.isoformat()}",
        }
    except Exception as e:
        log.warning(f"Cost forecast failed: {e}")
        return None


# ── CloudWatch cost driver correlation ──────────────────────────────────────

# Maps CE service names → how to fetch the CloudWatch metrics that explain billing.
# Each entry: (boto3_client, describe_method, id_extractor, cw_namespace, metrics_with_dims)
# This is the key to answering "ALB cost went up — because request count doubled".
_SERVICE_COST_DRIVERS = {
    "Amazon Elastic Load Balancing": {
        "discover": lambda region: _discover_alb_resources(region),
        "cw_namespace": "AWS/ApplicationELB",
        "metrics": ["RequestCount", "ProcessedBytes", "ConsumedLCUs", "ActiveConnectionCount", "NewConnectionCount"],
        "stat": "Sum",
    },
    "Amazon Relational Database Service": {
        "discover": lambda region: _discover_rds_resources(region),
        "cw_namespace": "AWS/RDS",
        "metrics": ["DatabaseConnections", "ReadIOPS", "WriteIOPS", "FreeStorageSpace", "CPUUtilization"],
        "stat": "Average",
    },
    "Amazon ElastiCache": {
        "discover": lambda region: _discover_elasticache_resources(region),
        "cw_namespace": "AWS/ElastiCache",
        "metrics": ["CurrConnections", "NetworkBytesIn", "NetworkBytesOut", "CPUUtilization"],
        "stat": "Average",
    },
    "EC2 - Other": {
        "discover": lambda region: _discover_nat_resources(region),
        "cw_namespace": "AWS/NATGateway",
        "metrics": ["BytesInFromSource", "BytesOutToDestination", "PacketsInFromSource", "ActiveConnectionCount"],
        "stat": "Sum",
    },
    "Amazon Virtual Private Cloud": {
        "discover": lambda region: _discover_nat_resources(region),
        "cw_namespace": "AWS/NATGateway",
        "metrics": ["BytesInFromSource", "BytesOutToDestination"],
        "stat": "Sum",
    },
}


def _discover_alb_resources(region: str) -> list[dict]:
    """Discover ALBs and return CloudWatch dimension sets."""
    import boto3
    client = boto3.client("elbv2", region_name=region)
    paginator = client.get_paginator("describe_load_balancers")
    results = []
    for page in paginator.paginate():
        for lb in page["LoadBalancers"]:
            if lb["Type"] == "application":
                # CloudWatch dimension: LoadBalancer = app/name/id (strip arn prefix)
                arn = lb["LoadBalancerArn"]
                # arn:aws:elasticloadbalancing:region:account:loadbalancer/app/name/id
                dim_value = "/".join(arn.split("loadbalancer/")[1:]) if "loadbalancer/" in arn else arn
                results.append({
                    "name": lb["LoadBalancerName"],
                    "dimensions": [{"Name": "LoadBalancer", "Value": dim_value}],
                })
    return results


def _discover_rds_resources(region: str) -> list[dict]:
    """Discover RDS instances and return CloudWatch dimension sets.
    Sorted by instance size (largest first) so the cap-at-5 gets the most expensive ones."""
    import boto3
    client = boto3.client("rds", region_name=region)
    paginator = client.get_paginator("describe_db_instances")
    instances = []
    for page in paginator.paginate():
        instances.extend(page["DBInstances"])
    # Sort: writer instances first, then by instance class descending (larger = more expensive)
    instances.sort(key=lambda i: (
        0 if not i.get("ReadReplicaSourceDBInstanceIdentifier") else 1,
        i.get("DBInstanceClass", ""),
    ), reverse=True)
    return [
        {
            "name": f"{inst['DBInstanceIdentifier']} ({inst.get('DBInstanceClass', '?')})",
            "dimensions": [{"Name": "DBInstanceIdentifier", "Value": inst["DBInstanceIdentifier"]}],
        }
        for inst in instances
    ]


def _discover_elasticache_resources(region: str) -> list[dict]:
    """Discover ElastiCache clusters and return CloudWatch dimension sets."""
    import boto3
    client = boto3.client("elasticache", region_name=region)
    paginator = client.get_paginator("describe_cache_clusters")
    clusters = []
    for page in paginator.paginate():
        clusters.extend(page["CacheClusters"])
    return [
        {
            "name": c["CacheClusterId"],
            "dimensions": [{"Name": "CacheClusterId", "Value": c["CacheClusterId"]}],
        }
        for c in clusters
    ]


def _discover_nat_resources(region: str) -> list[dict]:
    """Discover NAT Gateways and return CloudWatch dimension sets."""
    import boto3
    client = boto3.client("ec2", region_name=region)
    paginator = client.get_paginator("describe_nat_gateways")
    nats = []
    for page in paginator.paginate(Filter=[{"Name": "state", "Values": ["available"]}]):
        nats.extend(page["NatGateways"])
    return [
        {
            "name": n["NatGatewayId"],
            "dimensions": [{"Name": "NatGatewayId", "Value": n["NatGatewayId"]}],
        }
        for n in nats
    ]


def _fetch_cost_driver_metrics(service_name: str, region: str) -> str | None:
    """
    For an anomalous service, fetch the CloudWatch metrics that explain billing.
    Compares yesterday's metric values to the 7-day average.

    Returns a markdown string showing which operational metrics changed,
    or None if the service isn't in our mapping or discovery fails.
    """
    import boto3

    driver = _SERVICE_COST_DRIVERS.get(service_name)
    if not driver:
        return None

    try:
        resources = driver["discover"](region)
    except Exception as e:
        log.warning(f"Failed to discover resources for {service_name}: {e}")
        return None

    if not resources:
        return None

    cw = boto3.client("cloudwatch", region_name=region)
    end = datetime.now(timezone.utc)
    yesterday_start = end - timedelta(days=1)
    week_start = end - timedelta(days=8)

    lines = []
    for resource in resources[:5]:  # cap at 5 resources per service
        resource_lines = []
        for metric_name in driver["metrics"]:
            try:
                # Yesterday's value (1-day period)
                yesterday = cw.get_metric_statistics(
                    Namespace=driver["cw_namespace"],
                    MetricName=metric_name,
                    Dimensions=resource["dimensions"],
                    StartTime=yesterday_start,
                    EndTime=end,
                    Period=86400,
                    Statistics=[driver["stat"]],
                )
                # Prior 7 days — use daily periods so we can compute a proper daily average
                # (a single 7-day Sum would be 7x the daily value, not comparable to yesterday)
                week = cw.get_metric_statistics(
                    Namespace=driver["cw_namespace"],
                    MetricName=metric_name,
                    Dimensions=resource["dimensions"],
                    StartTime=week_start,
                    EndTime=yesterday_start,
                    Period=86400,
                    Statistics=[driver["stat"]],
                )

                yesterday_val = yesterday["Datapoints"][0][driver["stat"]] if yesterday["Datapoints"] else None

                # Average the daily datapoints from the prior week
                week_vals = [dp[driver["stat"]] for dp in week["Datapoints"]] if week["Datapoints"] else []
                week_avg = sum(week_vals) / len(week_vals) if week_vals else None

                if yesterday_val is None:
                    continue

                # Format values human-readable
                y_str = _format_metric_value(metric_name, yesterday_val)
                if week_avg and week_avg > 0:
                    pct = ((yesterday_val - week_avg) / week_avg) * 100
                    w_str = _format_metric_value(metric_name, week_avg)
                    flag = " **SPIKE**" if pct > 20 else ""
                    resource_lines.append(
                        f"  - {metric_name}: {y_str} (Baseline avg: {w_str}, {pct:+.1f}%){flag}"
                    )
                else:
                    resource_lines.append(f"  - {metric_name}: {y_str}")

            except Exception as e:
                log.debug(f"CloudWatch metric {metric_name} for {resource['name']} failed: {e}")
                continue

        if resource_lines:
            lines.append(f"**{resource['name']}**:")
            lines.extend(resource_lines)

    if not lines:
        return None

    return "\n".join(lines)


def _format_metric_value(metric_name: str, value: float) -> str:
    """Format a CloudWatch metric value with appropriate units."""
    name_lower = metric_name.lower()
    if "bytes" in name_lower:
        if value >= 1e9:
            return f"{value / 1e9:.1f} GB"
        elif value >= 1e6:
            return f"{value / 1e6:.1f} MB"
        else:
            return f"{value / 1e3:.1f} KB"
    elif "count" in name_lower or "connections" in name_lower or "iops" in name_lower:
        if value >= 1e6:
            return f"{value / 1e6:.1f}M"
        elif value >= 1e3:
            return f"{value / 1e3:.1f}K"
        else:
            return f"{value:.0f}"
    elif "utilization" in name_lower:
        return f"{value:.1f}%"
    elif "storage" in name_lower or "space" in name_lower:
        return f"{value / 1e9:.1f} GB"
    else:
        return f"{value:.2f}"


def _format_cost_tables(data: dict, threshold: float) -> tuple[str, list[dict], list[str]]:
    """
    Format cost data as markdown tables for LLM analysis.
    Returns (markdown_text, list_of_anomaly_dicts, list_of_anomaly_strings).

    Each anomaly dict: {type: "daily"|"service", name, cost, avg, pct_change}
    """
    daily = data["daily_totals"]
    avg = data["baseline_avg"]
    anomalies = []        # structured
    anomaly_strs = []     # human-readable

    # Latest day = yesterday (CE data has ~24h delay), day before = the one before that
    latest = data["dates_sorted"][-1]
    day_before = data["dates_sorted"][-2] if len(data["dates_sorted"]) >= 2 else None
    latest_total = daily[latest]
    day_before_total = daily[day_before] if day_before else 0
    dod_change = latest_total - day_before_total
    dod_pct = ((dod_change / day_before_total) * 100) if day_before_total > 0 else 0

    # Header with yesterday's date and day-over-day summary
    lines = [f"## AWS Cost Report — {latest} (latest available data)\n"]
    lines.append(f"**Yesterday ({latest}):** ${latest_total:.2f}")
    if day_before:
        lines.append(f"**Day before ({day_before}):** ${day_before_total:.2f}")
        lines.append(f"**Day-over-day change:** {dod_change:+.2f} ({dod_pct:+.1f}%)")
    lines.append(f"**Baseline avg:** ${avg:.2f}")
    lines.append("")

    # Daily totals table
    lines.append("### Daily Totals (last 7 days)")
    lines.append("| Date       | Total ($) | Day-over-Day | vs Baseline Avg |")
    lines.append("|------------|-----------|--------------|--------------|")

    prev_total = None
    for date in data["dates_sorted"][-7:]:
        total = daily[date]
        pct_avg = ((total - avg) / avg * 100) if avg > 0 else 0
        if prev_total is not None:
            dod = total - prev_total
            dod_p = ((dod / prev_total) * 100) if prev_total > 0 else 0
            dod_str = f"{dod_p:+.1f}%"
        else:
            dod_str = "—"
        flag = " <-- ANOMALY" if pct_avg > threshold * 100 else ""
        if pct_avg > threshold * 100:
            anomalies.append({
                "type": "daily", "name": date, "cost": total,
                "avg": avg, "pct_change": round(pct_avg, 1),
            })
            anomaly_strs.append(f"{date}: Total ${total:.2f} exceeds Baseline avg ${avg:.2f} by {pct_avg:.1f}%")
        lines.append(f"| {date} | {total:.2f} | {dod_str} | {pct_avg:+.1f}% |{flag}")
        prev_total = total

    # Top services — yesterday vs day-before + Baseline avg
    svc_costs = data["service_costs"]
    svc_avgs = data["service_avgs"]

    svc_yesterday = []
    for svc, date_costs in svc_costs.items():
        cost = date_costs.get(latest, 0)
        if cost >= 0.10:
            svc_yesterday.append((svc, cost))
    svc_yesterday.sort(key=lambda x: -x[1])

    lines.append(f"\n### Top Services — {latest} vs {day_before or 'N/A'}")
    lines.append("| Service | Yesterday ($) | Day Before ($) | Day-over-Day | Baseline Avg ($) | vs Avg |")
    lines.append("|---------|---------------|----------------|--------------|---------------|--------|")

    for svc, cost in svc_yesterday[:15]:
        svc_avg = svc_avgs.get(svc, 0)
        pct_avg = ((cost - svc_avg) / svc_avg * 100) if svc_avg > 0 else 0
        dollar_diff = cost - svc_avg
        # Day-before cost for this service
        db_cost = svc_costs.get(svc, {}).get(day_before, 0) if day_before else 0
        dod_svc = ((cost - db_cost) / db_cost * 100) if db_cost > 0 else 0
        dod_dollar = cost - db_cost

        flag = " <-- ANOMALY" if pct_avg > threshold * 100 and cost >= 1.0 else ""
        if pct_avg > threshold * 100 and cost >= 1.0:
            anomalies.append({
                "type": "service", "name": svc, "cost": round(cost, 2),
                "avg": round(svc_avg, 2), "pct_change": round(pct_avg, 1),
                "dollar_increase": round(dollar_diff, 2),
            })
            anomaly_strs.append(f"{svc}: ${cost:.2f} vs avg ${svc_avg:.2f} ({pct_avg:+.1f}%, +${dollar_diff:.2f})")
        lines.append(f"| {svc} | {cost:.2f} | {db_cost:.2f} | {dod_svc:+.1f}% ({dod_dollar:+.2f}) | {svc_avg:.2f} | {pct_avg:+.1f}% |{flag}")

    # Cost composition — % of total spend
    total_latest = daily.get(latest, 1)
    lines.append(f"\n### Cost Composition ({latest}) — What's Eating Your Budget")
    lines.append("| Service | Cost ($) | % of Total |")
    lines.append("|---------|----------|------------|")
    for svc, cost in svc_yesterday[:10]:
        pct_of_total = (cost / total_latest * 100) if total_latest > 0 else 0
        lines.append(f"| {svc} | {cost:.2f} | {pct_of_total:.1f}% |")

    # Week-over-week trend
    lines.append(f"\n### Week-over-Week Trend")
    lines.append(f"- Last 7 days total: ${data['last_7_total']:.2f}")
    lines.append(f"- Prior 7 days total: ${data['prior_7_total']:.2f}")
    wow = data['wow_pct']
    direction = "UP" if wow > 0 else "DOWN" if wow < 0 else "FLAT"
    lines.append(f"- Change: {wow:+.1f}% ({direction})")
    if abs(wow) > threshold * 100:
        lines.append(f"- **ALERT: Week-over-week change exceeds {threshold*100:.0f}% threshold**")

    # Month-over-month: current month daily avg vs prior month daily avg
    # Uses the 30-day data we already have — no extra API call
    today_date = datetime.now(timezone.utc).date()
    cur_month_prefix = today_date.strftime("%Y-%m")
    cur_month_days = {d: daily[d] for d in data["dates_sorted"] if d.startswith(cur_month_prefix)}
    prev_month_days = {d: daily[d] for d in data["dates_sorted"] if not d.startswith(cur_month_prefix)}
    if cur_month_days and prev_month_days:
        cur_avg_daily = sum(cur_month_days.values()) / len(cur_month_days)
        prev_avg_daily = sum(prev_month_days.values()) / len(prev_month_days)
        mom_pct = ((cur_avg_daily - prev_avg_daily) / prev_avg_daily * 100) if prev_avg_daily > 0 else 0
        lines.append(f"\n### Month-over-Month Trend")
        lines.append(f"- Current month avg daily spend: ${cur_avg_daily:.2f}/day ({len(cur_month_days)} days)")
        lines.append(f"- Prior month avg daily spend: ${prev_avg_daily:.2f}/day ({len(prev_month_days)} days)")
        mom_dir = "UP" if mom_pct > 0 else "DOWN" if mom_pct < 0 else "FLAT"
        lines.append(f"- Change: {mom_pct:+.1f}% ({mom_dir})")
        if mom_pct > 10:
            lines.append(f"- **Monthly spend is trending {mom_pct:.0f}% higher than last month**")

    # Day-by-day trend for anomalous services (last 10 days) — shows gradual vs sudden
    anomalous_svc_names = [a["name"] for a in anomalies if a["type"] == "service"]
    if anomalous_svc_names:
        recent_dates = data["dates_sorted"][-10:]
        lines.append("\n### Daily Trend for Anomalous Services (last 10 days)")
        lines.append("_Shows whether the spike was sudden (one day) or gradual (trending up)._\n")
        for svc in anomalous_svc_names:
            svc_data = data["service_costs"].get(svc, {})
            lines.append(f"**{svc}:**")
            trend_parts = []
            for d in recent_dates:
                cost = svc_data.get(d, 0)
                trend_parts.append(f"{d[-5:]}: ${cost:.2f}")
            lines.append("  " + " → ".join(trend_parts))
            lines.append("")

    if anomaly_strs:
        lines.append("\n### Anomalies Detected")
        for a in anomaly_strs:
            lines.append(f"- {a}")
    else:
        lines.append("\n### No anomalies detected — all services within normal range.")

    return "\n".join(lines), anomalies, anomaly_strs


def _analyze_costs(tables_md: str, anomalies: list[dict], anomaly_strs: list[str], llm) -> tuple[str, str]:
    """
    LLM analyzes cost data. Returns (markdown_analysis, severity).
    severity: "info" (normal), "warning" (elevated), "critical" (spike >30%).
    """
    severity = "info"
    if anomalies:
        severity = "warning"
        if any(a["pct_change"] >= 30 for a in anomalies):
            severity = "critical"

    if not anomalies:
        prompt = (
            f"{tables_md}\n\n"
            "Analyze these AWS costs. The user wants to understand where money is going and what changed.\n\n"
            "IMPORTANT: If today's data is PARTIAL (see 'Today vs Yesterday — Hourly Comparison' section), "
            "use the PROJECTED full-day cost for today, not the partial amount. Compare today's projected "
            "cost vs yesterday's actual cost for each service. Explain which services are trending higher "
            "or lower and why.\n\n"
            "Format your response as:\n"
            "## Summary\n<2-3 sentences: yesterday's total, today's projected total, hourly burn rate comparison, month forecast>\n\n"
            "## Today vs Yesterday\n"
            "<If today is partial: show projected full-day total based on hourly rate. "
            "Compare today's hourly burn rate vs yesterday's. Is today tracking higher or lower?>\n\n"
            "## Service-by-Service Analysis\n"
            "<For EVERY service (at least 10) from the per-service comparison table: "
            "show today's projected cost vs yesterday's actual cost, the $ and % change, "
            "and explain WHY it changed. Examples:\n"
            "- 'EC2 Compute: projected $340 today vs $320 yesterday (+$20, +6.3%) — higher traffic during business hours'\n"
            "- 'ELB: projected $275 today vs $272 yesterday (+$3, +1.1%) — stable, mostly fixed hourly charges'\n"
            "- 'RDS: projected $225 today vs $229 yesterday (-$4, -1.7%) — slightly lower I/O'\n"
            "Mark any service with >15% increase as needing attention.>\n\n"
            "## Month Outlook\n<projected month total from forecast, whether on track vs last month>\n"
        )
    else:
        # Anomalies found — deep analysis with 3 layers of data
        anomaly_detail = "\n".join(f"- {a}" for a in anomaly_strs)
        prompt = (
            f"{tables_md}\n\n"
            f"**{len(anomalies)} cost anomalies detected:**\n{anomaly_detail}\n\n"
            "You have 4 layers of data above for each anomalous service. Use ALL of them — do NOT guess:\n"
            "- **Usage Type Breakdown**: WHAT resource types changed (instance sizes, storage, data transfer)\n"
            "- **Operation Breakdown**: WHAT actions/events are generating charges\n"
            "- **CloudWatch Metrics**: The ACTUAL operational metrics driving the cost (request counts, connections, bytes, IOPS)\n"
            "- **Month Forecast**: WHERE spending is heading if this continues\n\n"
            "For EACH anomalous service, connect all 4 layers to tell the full story:\n"
            "1. **What increased in billing**: Cite exact usage types + operations with dollar amounts\n"
            "2. **What caused it operationally**: Use the CloudWatch metrics to explain WHY billing changed. "
            "Examples:\n"
            "   - 'ALB cost +$8/day: LCUUsage increased because RequestCount went from 1.2M to 1.8M/day (+50%), "
            "and ProcessedBytes went from 15GB to 22GB (+47%). This indicates a traffic increase to the cluster.'\n"
            "   - 'RDS cost +$12/day: A new db.r6g.2xlarge instance appeared (USAGE_TYPE), confirmed by "
            "CreateDBInstance operation. DatabaseConnections on existing instances also rose from 450 to 620 (+38%), "
            "suggesting the new instance was added to handle connection pressure.'\n"
            "   - 'NAT Gateway cost +$5/day: BytesOutToDestination increased from 8GB to 14GB (+75%). "
            "This means more traffic is flowing through NAT — likely a service making more external API calls.'\n"
            "3. **Dollar impact**: Daily + projected monthly from forecast data\n"
            "4. **Is this expected?**: Based on the metrics, is this a traffic growth (normal), "
            "a new resource (needs review), or a potential waste (needs immediate action)?\n"
            "5. **Action to take**: Specific steps tied to what the data shows\n\n"
            "Format your response as:\n"
            "## Summary\n<2-3 sentences: what spiked, the operational reason why, projected monthly impact>\n\n"
            "## Anomaly Breakdown\n"
            "<For each anomalous service: ### Service Name, then:\n"
            "**Billing change**: what usage types/operations changed\n"
            "**Root cause**: CloudWatch metrics that explain the billing change\n"
            "**Impact**: daily + monthly cost\n"
            "**Assessment**: expected growth vs waste vs needs investigation\n"
            "**Action**: specific next steps>\n\n"
            "## Month Forecast\n<projected total, sustainability assessment>\n\n"
            "## Recommendations\n<prioritized actionable items with expected savings>\n"
        )

    try:
        analysis = llm.summarize(prompt)
    except Exception as e:
        log.warning(f"LLM cost analysis failed: {e} — using raw tables as fallback")
        analysis = tables_md

    return analysis, severity


def _generate_and_post(config, force: bool = True, channel: str | None = None, thread_ts: str | None = None):
    """
    Orchestrator: fetch → analyze → PDF → Slack.

    force=True  (on-demand via @oogway costs): always post full report
    force=False (scheduled daily run):         only post if anomalies detected
    channel/thread_ts: if provided, post in that Slack thread with live status updates
    """
    if not _cost_report_lock.acquire(blocking=False):
        log.info("Cost report already running — skipping this invocation")
        return
    try:
        _generate_and_post_inner(config, force=force, channel=channel, thread_ts=thread_ts)
    finally:
        _cost_report_lock.release()


def _generate_and_post_inner(config, force: bool = True, channel: str | None = None, thread_ts: str | None = None):
    """Inner implementation — always called under _cost_report_lock."""
    cr = config.cost_report

    # Live status updater — posts progress to Slack thread like oracle streaming
    _status_client = None
    _status_ts = None
    _status_lines: list[str] = []

    def _status(msg: str):
        """Post a live status update to the Slack thread."""
        nonlocal _status_client, _status_ts
        _status_lines.append(msg)
        if not (channel and thread_ts and config.slack_bot_token):
            return
        try:
            if _status_client is None:
                from slack_sdk import WebClient
                _status_client = WebClient(token=config.slack_bot_token)
            visible = _status_lines[-10:]
            text = "\n".join(visible)
            if _status_ts is None:
                resp = _status_client.chat_postMessage(
                    channel=channel, thread_ts=thread_ts, text=text,
                    blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}],
                )
                _status_ts = resp["ts"]
            else:
                _status_client.chat_update(
                    channel=channel, ts=_status_ts, text=text,
                    blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": text}]}],
                )
        except Exception:
            pass

    today = datetime.now(timezone.utc).date().isoformat()

    # 1. Fetch cost data
    _status("⚙ `ce:GetCostAndUsage(30 days, by SERVICE)`")
    data = _fetch_cost_data(region=cr["region"])
    _status(f"⚙ `ce:GetCostAndUsage(30 days, by SERVICE)`  ✓ {len(data['service_costs'])} services")

    # 2. Format and detect anomalies
    tables_md, anomalies, anomaly_strs = _format_cost_tables(data, cr["anomaly_threshold"])
    if anomalies:
        _status(f"⚠ `{len(anomalies)} anomalies detected`")
    else:
        _status("✓ `No anomalies above threshold`")

    # 3. Skip if scheduled run and no anomalies
    if not force and not anomalies:
        log.info("Cost reporter: no anomalies detected — skipping Slack post")
        return

    # 4. For anomalous services, drill down with USAGE_TYPE + OPERATION breakdowns
    anomalous_services = [a["name"] for a in anomalies if a["type"] == "service"]
    usage_breakdowns = {}
    operation_breakdowns = {}

    for svc in anomalous_services:
        short_name = svc.replace("Amazon ", "").replace("AWS ", "")[:30]
        _status(f"⚙ `ce:USAGE_TYPE({short_name})`")
        try:
            breakdown = _fetch_usage_breakdown(svc, region=cr["region"])
            if breakdown:
                usage_breakdowns[svc] = breakdown
                _status(f"⚙ `ce:USAGE_TYPE({short_name})`  ✓ {len(breakdown)} types")
        except Exception as e:
            _status(f"⚙ `ce:USAGE_TYPE({short_name})`  ✗")
            log.warning(f"Failed to fetch usage breakdown for {svc}: {e}")

        _status(f"⚙ `ce:OPERATION({short_name})`")
        try:
            ops = _fetch_operation_breakdown(svc, region=cr["region"])
            if ops:
                operation_breakdowns[svc] = ops
                _status(f"⚙ `ce:OPERATION({short_name})`  ✓ {len(ops)} ops")
        except Exception as e:
            _status(f"⚙ `ce:OPERATION({short_name})`  ✗")
            log.warning(f"Failed to fetch operation breakdown for {svc}: {e}")

    # 5. Fetch CloudWatch metrics
    cloudwatch_drivers = {}
    for svc in anomalous_services:
        short_name = svc.replace("Amazon ", "").replace("AWS ", "")[:30]
        _status(f"⚙ `cloudwatch:metrics({short_name})`")
        try:
            metrics_md = _fetch_cost_driver_metrics(svc, region=cr["region"])
            if metrics_md:
                cloudwatch_drivers[svc] = metrics_md
                _status(f"⚙ `cloudwatch:metrics({short_name})`  ✓")
            else:
                _status(f"⚙ `cloudwatch:metrics({short_name})`  — no data")
        except Exception as e:
            _status(f"⚙ `cloudwatch:metrics({short_name})`  ✗")
            log.warning(f"Failed to fetch CloudWatch drivers for {svc}: {e}")

    # 6. Fetch hourly today vs yesterday comparison
    _status("⚙ `ce:GetCostAndUsage(HOURLY, today vs yesterday)`")
    hourly = None
    try:
        hourly = _fetch_hourly_comparison(region=cr["region"])
        if hourly:
            complete = "complete" if hourly["today_complete"] else f"partial ({hourly['today_hours']}h)"
            _status(f"⚙ `ce:HOURLY`  ✓ today={complete}, ${hourly['today_total_so_far']:.0f} so far")
    except Exception as e:
        _status("⚙ `ce:HOURLY`  ✗")
        log.warning(f"Hourly comparison failed: {e}")

    # 7. Fetch month-end cost forecast
    _status("⚙ `ce:GetCostForecast`")
    forecast = None
    try:
        forecast = _fetch_cost_forecast(region=cr["region"])
        if forecast:
            _status(f"⚙ `ce:GetCostForecast`  ✓ ${forecast['forecast_remaining']:.0f} remaining")
        else:
            _status("⚙ `ce:GetCostForecast`  — end of month")
    except Exception as e:
        _status("⚙ `ce:GetCostForecast`  ✗")
        log.warning(f"Cost forecast failed: {e}")

    # 6. Build enriched context for LLM
    if usage_breakdowns:
        tables_md += "\n\n## Usage Type Breakdown — What Resource Types Are Driving the Spike\n"
        for svc, breakdown in usage_breakdowns.items():
            tables_md += f"\n### {svc}\n"
            tables_md += "| Usage Type | Yesterday ($) | Baseline Avg ($) | $ Change | % Change |\n"
            tables_md += "|------------|---------------|---------------|----------|----------|\n"
            for item in breakdown:
                flag = ""
                if item["dollar_change"] > 0.50:
                    flag = " <-- INCREASE"
                elif item["avg_cost"] == 0 and item["yesterday_cost"] > 0:
                    flag = " <-- NEW"
                tables_md += (
                    f"| {item['usage_type']} | {item['yesterday_cost']:.2f} "
                    f"| {item['avg_cost']:.2f} | {item['dollar_change']:+.2f} "
                    f"| {item['pct_change']:+.1f}% |{flag}\n"
                )

    if operation_breakdowns:
        tables_md += "\n\n## Operation Breakdown — What Actions Are Costing Money\n"
        tables_md += (
            "_Operations show what API actions are generating charges. "
            "Examples: CreateDBInstance = new RDS, RunInstances = new EC2, "
            "PutObject = S3 writes, NatGateway = NAT traffic charges, "
            "CreateSnapshot = backup cost, ModifyDBInstance = instance resize._\n"
        )
        for svc, ops in operation_breakdowns.items():
            tables_md += f"\n### {svc}\n"
            tables_md += "| Operation | Yesterday ($) | Baseline Avg ($) | $ Change | % Change |\n"
            tables_md += "|-----------|---------------|---------------|----------|----------|\n"
            for item in ops:
                flag = ""
                if item["dollar_change"] > 0.50:
                    flag = " <-- INCREASE"
                elif item["avg_cost"] == 0 and item["yesterday_cost"] > 0:
                    flag = " <-- NEW"
                tables_md += (
                    f"| {item['operation']} | {item['yesterday_cost']:.2f} "
                    f"| {item['avg_cost']:.2f} | {item['dollar_change']:+.2f} "
                    f"| {item['pct_change']:+.1f}% |{flag}\n"
                )

    if cloudwatch_drivers:
        tables_md += "\n\n## CloudWatch Metrics — What's Actually Driving the Cost\n"
        tables_md += (
            "_These are the real operational metrics behind the billing. "
            "If RequestCount doubled, that's WHY ALB cost doubled. "
            "If DatabaseConnections spiked, that's WHY RDS cost went up._\n"
        )
        for svc, metrics_md in cloudwatch_drivers.items():
            tables_md += f"\n### {svc}\n{metrics_md}\n"

    if forecast:
        tables_md += (
            f"\n\n## Month Forecast\n"
            f"- Forecasted remaining spend ({forecast['period']}): **${forecast['forecast_remaining']:.2f}**\n"
            f"- Days remaining: {forecast['days_remaining']}\n"
            f"- Projected daily avg: ${forecast['mean_daily_forecast']:.2f}/day\n"
        )
        # Compute estimated month total: actual so far + forecast remaining
        today = datetime.now(timezone.utc).date()
        days_elapsed = today.day
        actual_so_far = sum(
            v for k, v in data["daily_totals"].items()
            if k.startswith(today.strftime("%Y-%m"))
        )
        est_month_total = actual_so_far + forecast["forecast_remaining"]
        tables_md += f"- Actual spend this month so far: ${actual_so_far:.2f} ({days_elapsed} days)\n"
        tables_md += f"- **Estimated month total: ${est_month_total:.2f}**\n"

    # Add hourly today vs yesterday comparison
    if hourly:
        status_label = "COMPLETE" if hourly["today_complete"] else f"PARTIAL ({hourly['today_hours']}h of data)"
        tables_md += f"\n\n## Today vs Yesterday — Hourly Comparison\n"
        tables_md += f"**Today ({hourly['today_date']}): {status_label}**\n"
        tables_md += f"- Spend so far: ${hourly['today_total_so_far']:.2f} ({hourly['today_hours']} hours)\n"
        tables_md += f"- Hourly burn rate: ${hourly['hourly_rate_today']:.2f}/hr\n"
        if hourly.get("note"):
            tables_md += f"- _{hourly['note']}_\n"
        elif not hourly["today_complete"] and hourly["today_projected"] is not None:
            tables_md += f"- **Projected full day: ${hourly['today_projected']:.2f}**\n"
        tables_md += f"\n**Yesterday ({hourly['yesterday_date']}): COMPLETE**\n"
        tables_md += f"- Total: ${hourly['yesterday_total']:.2f} ({hourly['yesterday_hours']} hours)\n"
        tables_md += f"- Hourly burn rate: ${hourly['hourly_rate_yesterday']:.2f}/hr\n"
        rate_diff = hourly["hourly_rate_today"] - hourly["hourly_rate_yesterday"]
        rate_pct = ((rate_diff / hourly["hourly_rate_yesterday"]) * 100) if hourly["hourly_rate_yesterday"] > 0 else 0
        tables_md += f"\n**Hourly rate change: {rate_diff:+.2f}/hr ({rate_pct:+.1f}%)**\n"

        tables_md += f"\n### Per-Service: Today (projected) vs Yesterday\n"
        tables_md += "| Service | Today So Far ($) | Projected ($) | Yesterday ($) | Change ($) | Change % |\n"
        tables_md += "|---------|------------------|---------------|---------------|------------|----------|\n"
        for sc in hourly["service_comparison"]:
            flag = ""
            proj_str = f"{sc['today_projected']:.2f}" if sc["today_projected"] is not None else "N/A"
            diff_str = f"{sc['diff']:+.2f}" if sc["diff"] is not None else "N/A"
            pct_str = f"{sc['pct_change']:+.1f}%" if sc["pct_change"] is not None else "N/A"
            if sc["pct_change"] is not None:
                if sc["pct_change"] > 15:
                    flag = " <-- UP"
                elif sc["pct_change"] < -15:
                    flag = " <-- DOWN"
            tables_md += (
                f"| {sc['service']} | {sc['today_so_far']:.2f} | {proj_str} "
                f"| {sc['yesterday']:.2f} | {diff_str} | {pct_str} |{flag}\n"
            )

    # LLM analysis — use the latest COMPLETE day for title
    # If today is partial, title should say yesterday's date
    latest_date = data["dates_sorted"][-1]
    if hourly and not hourly["today_complete"]:
        # Today is partial — use yesterday as main reference
        title = f"AWS Cost Report — {hourly['yesterday_date']} (+ today partial)"
    else:
        title = f"AWS Cost Report — {latest_date}"
    if anomalies:
        title = f"AWS Cost Alert — {len(anomalies)} anomalies — {latest_date}"

    _status("⚙ `LLM analysis`")
    llm = config.make_llm()
    analysis, severity = _analyze_costs(tables_md, anomalies, anomaly_strs, llm)
    _status("⚙ `LLM analysis`  ✓")

    # 8. Generate PDF
    _status("⚙ `Generating PDF`")
    pdf_path = None
    try:
        from vishwakarma.bot.pdf import generate_pdf
        pdf_path = generate_pdf(
            title=title,
            analysis=analysis,
            source="cost-explorer",
            severity=severity,
            output_path=f"/data/cost_report_{today}.pdf",
        )
        _status("⚙ `Generating PDF`  ✓")
    except Exception as e:
        _status("⚙ `Generating PDF`  ✗")
        log.warning(f"PDF generation failed: {e}")

    # 9. Finalize status and post to Slack
    step_count = len(_status_lines)
    if _status_ts and _status_client:
        try:
            _status_client.chat_update(
                channel=channel, ts=_status_ts,
                text=f"📊 _{step_count} steps · done_",
                blocks=[{"type": "context", "elements": [{"type": "mrkdwn", "text": f"📊 _{step_count} steps · done_"}]}],
            )
        except Exception:
            pass

    try:
        from vishwakarma.plugins.relays.slack.plugin import SlackDestination
        dest = SlackDestination({"token": config.slack_bot_token})
        dest.post_investigation(
            title=title,
            analysis=analysis,
            severity=severity,
            source="cost-explorer",
            channel=channel or cr["channel"] or None,
            thread_ts=thread_ts,
            pdf_path=pdf_path,
        )
        log.info(f"Cost report posted to Slack (severity={severity}, anomalies={len(anomalies)})")
    except Exception as e:
        log.error(f"Failed to post cost report to Slack: {e}")
        if pdf_path:
            log.info(f"PDF still available at {pdf_path}")

    # 10. For critical anomalies, auto-trigger a full agentic investigation
    #     The cost report tells you WHAT spiked. The investigation tells you WHY
    #     by checking CloudTrail, recent deployments, CloudWatch metrics, etc.
    if severity == "critical":
        _trigger_cost_investigation(config, anomalies, tables_md)


def _trigger_cost_investigation(config, anomalies: list[dict], cost_context: str):
    """
    For critical cost spikes, run a full agentic investigation to find the operational
    root cause. The cost report gives the 'what' — this gives the 'why' by using
    CloudTrail, kubectl, aws CLI, etc.
    """
    if _investigation_running.is_set():
        log.info("Cost investigation already running — skipping")
        return

    # Build a focused investigation question from the anomalies
    svc_anomalies = [a for a in anomalies if a["type"] == "service"]
    if not svc_anomalies:
        return

    # Pick the top 3 anomalous services by dollar increase
    svc_anomalies.sort(key=lambda a: -a.get("dollar_increase", a.get("pct_change", 0)))
    top = svc_anomalies[:3]

    svc_list = ", ".join(a["name"] for a in top)
    details = []
    for a in top:
        inc = a.get("dollar_increase", 0)
        details.append(f"- {a['name']}: ${a['cost']:.2f}/day vs avg ${a['avg']:.2f} ({a['pct_change']:+.1f}%, +${inc:.2f}/day)")
    details_str = "\n".join(details)

    question = (
        f"CRITICAL AWS cost anomaly detected. Investigate WHY these services spiked:\n\n"
        f"{details_str}\n\n"
        f"Investigation steps:\n"
        f"1. Check CloudTrail for recent API calls (CreateDBInstance, RunInstances, ModifyDBInstance, "
        f"PutBucketPolicy, etc.) in the last 48 hours for these services\n"
        f"2. Check for recent K8s deployments/scaling events: kubectl get events --sort-by=.lastTimestamp\n"
        f"3. Check if any new EC2/RDS instances were launched: aws ec2 describe-instances / aws rds describe-db-instances\n"
        f"4. Check CloudWatch for traffic spikes that correlate with cost increase\n"
        f"5. Check if any Reserved Instances or Savings Plans expired recently\n\n"
        f"Determine: Was this an intentional change (deployment, scaling), an operational issue "
        f"(stuck scaling, orphaned resource), or expected growth (traffic increase)?\n\n"
        f"## Pre-computed Cost Data\n{cost_context[:3000]}"
    )

    def _run():
        _investigation_running.set()
        try:
            log.info(f"Auto-investigating critical cost anomaly: {svc_list}")
            llm = config.make_llm()
            tm = config.make_toolset_manager()
            tm.check_all()
            engine = config.make_engine(llm=llm, toolset_manager=tm)
            result = engine.investigate(question=question)

            investigation_analysis = result.answer or "(no analysis)"

            # Post investigation result to Slack as a follow-up
            cr = config.cost_report
            try:
                from vishwakarma.bot.pdf import generate_pdf
                today = datetime.now(timezone.utc).date().isoformat()
                inv_pdf = generate_pdf(
                    title=f"Cost Anomaly Investigation — {today}",
                    analysis=investigation_analysis,
                    source="cost-investigation",
                    severity="critical",
                    tool_outputs=[o.model_dump() for o in result.tool_outputs],
                    meta=result.meta.model_dump() if result.meta else {},
                    output_path=f"/data/cost_investigation_{today}.pdf",
                )
            except Exception:
                inv_pdf = None

            from vishwakarma.plugins.relays.slack.plugin import SlackDestination
            dest = SlackDestination({"token": config.slack_bot_token})
            dest.post_investigation(
                title=f"Cost Anomaly Investigation — {svc_list}",
                analysis=investigation_analysis,
                severity="critical",
                source="cost-investigation",
                channel=cr["channel"] or None,
                pdf_path=inv_pdf,
            )
            log.info("Cost anomaly investigation completed and posted to Slack")
        except Exception:
            log.exception("Cost anomaly auto-investigation failed")
        finally:
            _investigation_running.clear()

    threading.Thread(target=_run, daemon=True, name="cost-investigation").start()
