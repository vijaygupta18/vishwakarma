"""
Vishwakarma CLI — the `vk` command.

Usage:
  vk probe "why are payments pods crashing?"
  vk scan alertmanager
  vk scan jira --jql "project=OPS AND priority=High"
  vk arsenal list
  vk incidents list
  vk serve
  vk oracle
"""
import json
import logging
import sys
from pathlib import Path
from typing import Optional

import typer
from rich.console import Console
from rich.table import Table
from rich import print as rprint

app = typer.Typer(
    name="vk",
    help="Vishwakarma — Autonomous SRE Investigation Agent",
    no_args_is_help=True,
)
console = Console()

# Sub-command groups
scan_app = typer.Typer(help="Scan alert sources and investigate issues")
arsenal_app = typer.Typer(help="Manage toolsets (your investigation arsenal)")
incidents_app = typer.Typer(help="View stored incidents from SQLite")

app.add_typer(scan_app, name="scan")
app.add_typer(arsenal_app, name="arsenal")
app.add_typer(incidents_app, name="incidents")


def _load_config(config_path: Optional[str] = None):
    from vishwakarma.config import VishwakarmaConfig
    return VishwakarmaConfig.load(config_path)


# ── vk probe ──────────────────────────────────────────────────────────────────

@app.command()
def probe(
    question: str = typer.Argument(..., help="What to investigate"),
    config: Optional[str] = typer.Option(None, "--config", "-c", help="Path to config.yaml"),
    stream: bool = typer.Option(False, "--stream", "-s", help="Stream output in real time"),
    file: Optional[str] = typer.Option(None, "--file", "-f", help="Attach a file to the investigation"),
    show_tools: bool = typer.Option(False, "--show-tools", help="Show tool call details"),
    bash_allow: bool = typer.Option(False, "--bash-allow", help="Allow all bash commands"),
    bash_block: bool = typer.Option(False, "--bash-block", help="Block all bash commands"),
    max_steps: int = typer.Option(40, "--max-steps", help="Max investigation steps"),
    output_json: bool = typer.Option(False, "--json", help="Output result as JSON"),
    pdf: Optional[str] = typer.Option(None, "--pdf", help="Generate PDF report at this path"),
):
    """Investigate a question using all available tools."""
    cfg = _load_config(config)
    cfg.max_steps = max_steps

    # Read file content if provided
    files = None
    if file:
        try:
            files = [Path(file).read_text()]
        except Exception as e:
            console.print(f"[red]Cannot read file {file}: {e}[/red]")
            raise typer.Exit(1)

    # Read stdin if piped
    if not sys.stdin.isatty():
        stdin_content = sys.stdin.read().strip()
        if stdin_content:
            files = (files or []) + [stdin_content]

    tm = cfg.make_toolset_manager()
    tm.check_all()

    if stream:
        _probe_stream(cfg, tm, question, files, show_tools, bash_allow, bash_block)
    else:
        _probe_sync(cfg, tm, question, files, show_tools, bash_allow, bash_block, output_json, pdf)


def _probe_sync(cfg, tm, question, files, show_tools, bash_allow, bash_block, output_json, pdf_path):
    from vishwakarma.utils.colors import AI_COLOR, TOOL_COLOR, DIM_COLOR

    with console.status("[bold green]Investigating...[/bold green]"):
        llm = cfg.make_llm()
        engine = cfg.make_engine(llm=llm, toolset_manager=tm)
        result = engine.investigate(
            question=question,
            files=files,
            bash_always_allow=bash_allow,
            bash_always_deny=bash_block,
        )

    if output_json:
        print(json.dumps({
            "analysis": result.answer,
            "tool_outputs": [o.model_dump() for o in result.tool_outputs],
            "meta": result.meta.model_dump() if result.meta else {},
        }, indent=2))
        return

    console.print()
    console.rule("[bold]Investigation Result[/bold]")
    console.print(result.answer or "(no analysis)", markup=False)

    if show_tools and result.tool_outputs:
        console.rule("[dim]Tool Outputs[/dim]")
        for out in result.tool_outputs:
            status_color = "green" if out.status.value == "success" else "red"
            console.print(f"[{status_color}]▶ {out.invocation}[/{status_color}]")
            if out.output:
                console.print(str(out.output)[:500], markup=False)

    if result.meta:
        m = result.meta
        console.print(
            f"\n[dim]Steps: {m.steps_taken} | Model: {m.model} | "
            f"Cost: ${m.total_cost:.4f} | Duration: {m.duration_seconds}s[/dim]"
        )

    if pdf_path:
        _generate_pdf_cli(question, result, pdf_path)


def _probe_stream(cfg, tm, question, files, show_tools, bash_allow, bash_block):
    llm = cfg.make_llm()
    engine = cfg.make_engine(llm=llm, toolset_manager=tm)

    console.print(f"[bold green]Probing:[/bold green] {question}\n")

    for event in engine.stream_investigate(
        question=question,
        bash_always_allow=bash_allow,
        bash_always_deny=bash_block,
    ):
        etype = event.get("type", "")
        if etype == "text_delta":
            print(event.get("content", ""), end="", flush=True)
        elif etype == "tool_call_start" and show_tools:
            console.print(f"\n[cyan]  ⚙ {event.get('tool')}()[/cyan]")
        elif etype == "tool_call_result" and show_tools:
            st = event.get("status", "")
            color = "green" if st == "success" else "red"
            console.print(f"[{color}]    ✓ {st}[/{color}]")
        elif etype == "done":
            content = event.get("content", "")
            if content:
                print(content, flush=True)
            print()
        elif etype == "error":
            console.print(f"\n[red]Error: {event.get('message')}[/red]")


def _generate_pdf_cli(question, result, pdf_path):
    try:
        from vishwakarma.bot.pdf import generate_pdf
        path = generate_pdf(
            title=question[:80],
            analysis=result.answer or "",
            tool_outputs=[o.model_dump() for o in result.tool_outputs],
            meta=result.meta.model_dump() if result.meta else {},
            output_path=pdf_path,
        )
        if path:
            console.print(f"[green]PDF saved to {path}[/green]")
        else:
            console.print("[red]PDF generation failed (install weasyprint)[/red]")
    except Exception as e:
        console.print(f"[red]PDF error: {e}[/red]")


# ── vk oracle (interactive mode) ──────────────────────────────────────────────

@app.command()
def oracle(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    resume: Optional[str] = typer.Option(None, "--resume", "-r", help="Resume a previous session by ID"),
    sessions: bool = typer.Option(False, "--sessions", help="List recent oracle sessions"),
):
    """Start an interactive multi-turn investigation session."""
    cfg = _load_config(config)

    if sessions:
        from vishwakarma.storage.queries import list_oracle_sessions
        from vishwakarma.storage.db import init_db
        import datetime
        init_db(cfg.db_path)
        rows = list_oracle_sessions(limit=20)
        if not rows:
            typer.echo("No oracle sessions found.")
            return
        console = Console()
        table = Table(title="Oracle Sessions", show_lines=True)
        table.add_column("ID", style="cyan", no_wrap=True)
        table.add_column("Title")
        table.add_column("Updated", style="dim")
        for row in rows:
            updated = datetime.datetime.fromtimestamp(row["updated_at"]).strftime("%Y-%m-%d %H:%M")
            table.add_row(row["id"][:16] + "...", row["title"][:60], updated)
        console.print(table)
        typer.echo("\nResume with: vk oracle --resume <full-id>")
        return

    from vishwakarma.interactive import InteractiveSession
    session = InteractiveSession(cfg, session_id=resume)
    session.run()


# ── vk scan ───────────────────────────────────────────────────────────────────

@scan_app.command("alertmanager")
def scan_alertmanager(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    name: Optional[str] = typer.Option(None, "--name", "-n", help="Filter by alertname (regex)"),
    label: Optional[str] = typer.Option(None, "--label", "-l", help="Filter by label key=value"),
    limit: int = typer.Option(10, "--limit", help="Max alerts to investigate"),
    update: bool = typer.Option(False, "--update", help="Write results back (future)"),
):
    """Fetch firing alerts from AlertManager and investigate each."""
    cfg = _load_config(config)
    am_cfg = cfg.toolsets_config.get("alertmanager", {}).get("config", {})
    if not am_cfg.get("url"):
        console.print("[red]alertmanager.config.url not configured[/red]")
        raise typer.Exit(1)

    from vishwakarma.plugins.channels.alertmanager.plugin import AlertManagerSource
    source = AlertManagerSource(am_cfg)
    issues = source.fetch_issues()

    if not issues:
        console.print("[yellow]No active alerts found.[/yellow]")
        return

    # Apply filters
    import re
    if name:
        issues = [i for i in issues if re.search(name, i.title, re.IGNORECASE)]
    if label:
        k, v = label.split("=", 1) if "=" in label else (label, "")
        issues = [i for i in issues if i.labels.get(k) == v]

    issues = issues[:limit]
    console.print(f"[bold]Investigating {len(issues)} alert(s)...[/bold]\n")

    tm = cfg.make_toolset_manager()
    tm.check_all()

    for issue in issues:
        console.rule(f"[bold]{issue.title}[/bold]")
        llm = cfg.make_llm()
        engine = cfg.make_engine(llm=llm, toolset_manager=tm)

        with console.status(f"Investigating {issue.title}..."):
            result = engine.investigate(question=issue.question())

        console.print(result.answer or "(no analysis)", markup=False)
        console.print()


@scan_app.command("jira")
def scan_jira(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    jql: Optional[str] = typer.Option(None, "--jql", help="JQL query"),
    update: bool = typer.Option(False, "--update", help="Write RCA back to Jira ticket"),
    limit: int = typer.Option(5, "--limit"),
):
    """Fetch Jira issues and investigate each."""
    cfg = _load_config(config)
    jira_cfg = cfg.toolsets_config.get("jira", {}).get("config", {})

    from vishwakarma.plugins.channels.jira.plugin import JiraSource
    source = JiraSource(jira_cfg)
    issues = source.fetch_issues(jql=jql)[:limit]

    if not issues:
        console.print("[yellow]No Jira issues found.[/yellow]")
        return

    tm = cfg.make_toolset_manager()
    tm.check_all()

    for issue in issues:
        console.rule(f"[bold]{issue.title}[/bold]")
        llm = cfg.make_llm()
        engine = cfg.make_engine(llm=llm, toolset_manager=tm)
        with console.status(f"Investigating {issue.title}..."):
            result = engine.investigate(question=issue.question())
        console.print(result.answer or "(no analysis)", markup=False)

        if update:
            key = issue.labels.get("issue_key", "")
            if key and source.write_back(key, result.answer or ""):
                console.print(f"[green]✓ Written back to {key}[/green]")
        console.print()


@scan_app.command("pagerduty")
def scan_pagerduty(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    update: bool = typer.Option(False, "--update", help="Add note to PagerDuty incident"),
    limit: int = typer.Option(5, "--limit"),
):
    """Fetch PagerDuty incidents and investigate each."""
    cfg = _load_config(config)
    pd_cfg = cfg.toolsets_config.get("pagerduty", {}).get("config", {})

    from vishwakarma.plugins.channels.pagerduty.plugin import PagerDutySource
    source = PagerDutySource(pd_cfg)
    issues = source.fetch_issues()[:limit]

    if not issues:
        console.print("[yellow]No PagerDuty incidents found.[/yellow]")
        return

    tm = cfg.make_toolset_manager()
    tm.check_all()

    for issue in issues:
        console.rule(f"[bold]{issue.title}[/bold]")
        llm = cfg.make_llm()
        engine = cfg.make_engine(llm=llm, toolset_manager=tm)
        with console.status(f"Investigating..."):
            result = engine.investigate(question=issue.question())
        console.print(result.answer or "(no analysis)", markup=False)

        if update:
            inc_id = issue.id.replace("pagerduty:", "")
            if source.write_back(inc_id, result.answer or ""):
                console.print(f"[green]✓ Note added to PagerDuty[/green]")
        console.print()


# ── vk arsenal ────────────────────────────────────────────────────────────────

@arsenal_app.command("list")
def arsenal_list(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    check: bool = typer.Option(False, "--check", help="Run prerequisite checks"),
):
    """List all toolsets and their status."""
    cfg = _load_config(config)
    tm = cfg.make_toolset_manager()

    if check:
        with console.status("Checking toolset connectivity..."):
            tm.check_all(force=True)

    table = Table(title="Vishwakarma Arsenal (Toolsets)")
    table.add_column("Name", style="bold cyan")
    table.add_column("Status")
    table.add_column("Description")
    table.add_column("Tools")

    from vishwakarma.core.tools import ToolsetHealth
    for ts in tm.all_toolsets():
        health = ts.health
        status_str = health.value if health else "unchecked"
        status_color = "green" if health == ToolsetHealth.READY else "red" if health == ToolsetHealth.FAILED else "yellow"
        tools = ts.get_tools() if ts.enabled else []
        table.add_row(
            ts.name,
            f"[{status_color}]{status_str}[/{status_color}]",
            getattr(ts, "description", "")[:50],
            str(len(tools)),
        )

    console.print(table)


@arsenal_app.command("check")
def arsenal_check(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
):
    """Run prerequisite checks for all toolsets."""
    cfg = _load_config(config)
    tm = cfg.make_toolset_manager()
    with console.status("Checking all toolsets..."):
        results = tm.check_all(force=True)

    from vishwakarma.core.tools import ToolsetHealth
    for name, health in sorted(results.items()):
        color = "green" if health == ToolsetHealth.READY else "red" if health == ToolsetHealth.FAILED else "yellow"
        ts = tm.get(name)
        err = getattr(ts, "error", "") if ts else ""
        line = f"[{color}]{health.value:10}[/{color}] {name}"
        if err:
            line += f" — {err}"
        console.print(line)


# ── vk incidents ──────────────────────────────────────────────────────────────

@incidents_app.command("list")
def incidents_list(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    source: Optional[str] = typer.Option(None, "--source"),
    status: Optional[str] = typer.Option(None, "--status"),
    limit: int = typer.Option(20, "--limit"),
):
    """List recent incidents from storage."""
    cfg = _load_config(config)
    from vishwakarma.storage.db import init_db
    from vishwakarma.storage.queries import list_incidents
    init_db(cfg.db_path)
    incidents = list_incidents(source=source, status=status, limit=limit)

    if not incidents:
        console.print("[yellow]No incidents found.[/yellow]")
        return

    table = Table(title=f"Recent Incidents ({len(incidents)})")
    table.add_column("ID", style="dim", max_width=10)
    table.add_column("Title", max_width=50)
    table.add_column("Source")
    table.add_column("Severity")
    table.add_column("Status")
    table.add_column("Created")

    import datetime
    for inc in incidents:
        ts = datetime.datetime.fromtimestamp(inc["created_at"]).strftime("%m-%d %H:%M")
        table.add_row(
            inc["id"][:8],
            inc["title"][:50],
            inc.get("source", ""),
            inc.get("severity", ""),
            inc.get("status", ""),
            ts,
        )
    console.print(table)


@incidents_app.command("show")
def incidents_show(
    incident_id: str = typer.Argument(...),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
):
    """Show full details of an incident."""
    cfg = _load_config(config)
    from vishwakarma.storage.db import init_db
    from vishwakarma.storage.queries import get_incident
    init_db(cfg.db_path)
    inc = get_incident(incident_id)
    if not inc:
        console.print(f"[red]Incident {incident_id} not found[/red]")
        raise typer.Exit(1)

    console.rule(f"[bold]{inc['title']}[/bold]")
    console.print(f"Source: {inc.get('source')} | Severity: {inc.get('severity')} | Status: {inc.get('status')}")
    console.print()
    console.print("[bold]Question:[/bold]")
    console.print(inc.get("question", ""), markup=False)
    console.print()
    console.rule("[bold]Analysis[/bold]")
    console.print(inc.get("analysis", ""), markup=False)


@incidents_app.command("search")
def incidents_search(
    query: str = typer.Argument(...),
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    limit: int = typer.Option(10, "--limit"),
):
    """Search incidents by keyword."""
    cfg = _load_config(config)
    from vishwakarma.storage.db import init_db
    from vishwakarma.storage.queries import search_incidents
    init_db(cfg.db_path)
    results = search_incidents(query, limit=limit)
    if not results:
        console.print(f"[yellow]No incidents matching '{query}'[/yellow]")
        return
    for inc in results:
        console.print(f"[cyan]{inc['id'][:8]}[/cyan] {inc['title'][:60]} [{inc.get('source')}]")


@incidents_app.command("stats")
def incidents_stats(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
):
    """Show incident statistics."""
    cfg = _load_config(config)
    from vishwakarma.storage.db import init_db
    from vishwakarma.storage.queries import get_stats
    init_db(cfg.db_path)
    stats = get_stats()
    console.print(f"Total incidents: [bold]{stats['total']}[/bold]")
    console.print("\nBy status:")
    for k, v in stats.get("by_status", {}).items():
        console.print(f"  {k}: {v}")
    console.print("\nBy source:")
    for k, v in stats.get("by_source", {}).items():
        console.print(f"  {k}: {v}")


# ── vk serve ──────────────────────────────────────────────────────────────────

@app.command()
def serve(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
    host: Optional[str] = typer.Option(None, "--host"),
    port: Optional[int] = typer.Option(None, "--port"),
):
    """Start the Vishwakarma server + Slack bot."""
    import uvicorn
    cfg = _load_config(config)

    from vishwakarma.utils.log import setup_logging
    setup_logging()

    # Start Slack bot in background thread
    from vishwakarma.bot.slack import start_bot
    start_bot(cfg)

    # Start daily cost report scheduler
    from vishwakarma.scheduler.cost_report import start_cost_reporter
    start_cost_reporter(cfg)

    # Start FastAPI server
    from vishwakarma.server import create_app
    fastapi_app = create_app(cfg)

    uvicorn.run(
        fastapi_app,
        host=host or cfg.host,
        port=port or cfg.port,
        log_config=None,  # Use our logging setup
    )


# ── vk version + config ───────────────────────────────────────────────────────

@app.command()
def version():
    """Show Vishwakarma version."""
    import vishwakarma
    console.print(f"Vishwakarma v{vishwakarma.__version__}")


@app.command("config")
def show_config(
    config: Optional[str] = typer.Option(None, "--config", "-c"),
):
    """Show the active configuration (no secrets)."""
    cfg = _load_config(config)
    summary = cfg.summary()
    for k, v in summary.items():
        console.print(f"  [bold]{k}:[/bold] {v}")


def run():
    """Entry point for the `vk` command."""
    from vishwakarma.utils.log import setup_logging
    setup_logging(level="WARNING")  # quiet by default for CLI
    app()
