"""
Vishwakarma PDF Report Generator.

Produces a professional, branded RCA PDF report.
Design: SRE Platform orange/saffron palette, dark code blocks, clean typography.

Sections auto-detected from markdown analysis:
  - Full-page cover with severity badge + metadata
  - Root Cause Analysis (markdown rendered)
  - Evidence appendix (tool outputs with dark code blocks)
  - Per-page footer with page numbers
"""
import logging
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

log = logging.getLogger(__name__)

# ── Brand palette ──────────────────────────────────────────────────────────────
SEVERITY_COLORS = {
    "critical":  {"bg": "#DC2626", "light": "#FEF2F2", "border": "#FCA5A5"},
    "high":      {"bg": "#EA580C", "light": "#FFF7ED", "border": "#FED7AA"},
    "medium":    {"bg": "#D97706", "light": "#FFFBEB", "border": "#FDE68A"},
    "low":       {"bg": "#16A34A", "light": "#F0FDF4", "border": "#86EFAC"},
    "info":      {"bg": "#2563EB", "light": "#EFF6FF", "border": "#BFDBFE"},
    "unknown":   {"bg": "#6B7280", "light": "#F9FAFB", "border": "#D1D5DB"},
}

NY_ORANGE = "#FF6B00"
NY_DARK   = "#1C1C2E"
NY_GRAY   = "#64748B"
NY_LIGHT  = "#F8FAFC"
CODE_BG   = "#0D1117"
CODE_FG   = "#E6EDF3"

CSS = """
/* Google Fonts — loaded when internet is available; system fonts used as fallback in air-gapped/K8s envs */
@import url('https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap');

@page {
  size: A4;
  margin: 12mm 0 18mm 0;
  @bottom-center {
    content: counter(page) " / " counter(pages);
    font-family: 'Inter', sans-serif;
    font-size: 9px;
    color: #94A3B8;
    padding-bottom: 6mm;
  }
}
@page :first {
  margin-top: 0;  /* cover starts flush at top */
}

* { box-sizing: border-box; margin: 0; padding: 0; }

body {
  font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif;
  font-size: 12px;
  color: %(dark)s;
  background: #ffffff;
  line-height: 1.65;
}

/* ── Cover — full A4 page ── */
.cover {
  background: linear-gradient(135deg, %(dark)s 0%%, #16213E 55%%, #0F3460 100%%);
  color: white;
  padding: 40px 52px 32px;
  position: relative;
}
.cover::before {
  content: '';
  position: absolute;
  top: -80px; right: -80px;
  width: 300px; height: 300px;
  border-radius: 50%%;
  background: rgba(255,107,0,0.10);
}
.cover::after {
  content: '';
  position: absolute;
  bottom: -60px; left: -60px;
  width: 200px; height: 200px;
  border-radius: 50%%;
  background: rgba(255,107,0,0.06);
}
/* extra decorative circle */
.cover-circle-mid {
  position: absolute;
  top: 40%%; right: -30px;
  width: 120px; height: 120px;
  border-radius: 50%%;
  background: rgba(255,255,255,0.04);
}
.cover-top {
  display: flex;
  justify-content: space-between;
  align-items: flex-start;
  margin-bottom: 32px;
}
.brand {
  font-size: 11px;
  font-weight: 600;
  letter-spacing: 1.5px;
  text-transform: uppercase;
  color: %(orange)s;
}
.generated-at {
  font-size: 10px;
  color: rgba(255,255,255,0.5);
  text-align: right;
}
.cover-body {
  margin-bottom: 28px;
}
.severity-badge {
  display: inline-block;
  padding: 4px 14px;
  border-radius: 20px;
  font-size: 10px;
  font-weight: 700;
  letter-spacing: 0.8px;
  text-transform: uppercase;
  margin-bottom: 20px;
  background: %(sev_bg)s;
  color: white;
  box-shadow: 0 2px 8px rgba(0,0,0,0.3);
}
.cover-title {
  font-size: 26px;
  font-weight: 700;
  line-height: 1.25;
  color: white;
  margin-bottom: 24px;
  letter-spacing: -0.4px;
  max-width: 480px;
}
.cover-divider {
  width: 48px;
  height: 3px;
  background: %(orange)s;
  border-radius: 2px;
  margin-bottom: 24px;
}
.cover-meta {
  display: flex;
  gap: 28px;
  flex-wrap: wrap;
  border-top: 1px solid rgba(255,255,255,0.12);
  padding-top: 20px;
}
.meta-item {
  font-size: 10px;
  color: rgba(255,255,255,0.55);
}
.meta-item strong {
  display: block;
  font-size: 11px;
  color: rgba(255,255,255,0.9);
  font-weight: 600;
  margin-bottom: 2px;
}

/* ── Alert Banner (severity-themed) ── */
.alert-banner {
  margin: 0;
  padding: 11px 52px;
  font-size: 11px;
  font-weight: 500;
  background: %(sev_light)s;
  border-bottom: 2px solid %(sev_border)s;
  color: %(dark)s;
}

/* ── Body ── */
.body {
  padding: 36px 52px 48px;
}

/* ── Section headings ── */
h1, h2 {
  font-size: 14px;
  font-weight: 700;
  color: %(dark)s;
  margin: 28px 0 10px;
  padding-left: 12px;
  border-left: 3px solid %(orange)s;
  line-height: 1.3;
}
h1:first-child, h2:first-child { margin-top: 0; }
h3 {
  font-size: 12px;
  font-weight: 600;
  color: #374151;
  margin: 18px 0 6px;
}
h4 { font-size: 11px; font-weight: 600; color: %(gray)s; margin: 12px 0 4px; }

p { margin: 6px 0; color: #374151; }
a { color: %(orange)s; text-decoration: none; }
strong { color: %(dark)s; font-weight: 600; }
em { color: %(gray)s; }

/* ── Inline code ── */
code {
  font-family: 'JetBrains Mono', 'Courier New', monospace;
  font-size: 10.5px;
  background: #F1F5F9;
  color: #0F172A;
  padding: 2px 6px;
  border-radius: 4px;
  border: 1px solid #E2E8F0;
}

/* ── Code blocks ── */
pre {
  background: %(code_bg)s;
  color: %(code_fg)s;
  padding: 14px 16px;
  border-radius: 8px;
  font-family: 'JetBrains Mono', 'Courier New', monospace;
  font-size: 10px;
  line-height: 1.6;
  margin: 10px 0;
  border-left: 3px solid %(orange)s;
  white-space: pre-wrap;       /* wrap long lines — no horizontal overflow in PDF */
  word-wrap: break-word;
}
pre code {
  background: none;
  border: none;
  color: inherit;
  padding: 0;
  font-size: inherit;
  white-space: inherit;
}

/* ── Blockquote ── */
blockquote {
  border-left: 3px solid %(orange)s;
  background: %(sev_light)s;
  padding: 10px 16px;
  margin: 10px 0;
  border-radius: 0 6px 6px 0;
  color: #374151;
  font-style: italic;
}

/* ── Lists ── */
ul, ol { padding-left: 20px; margin: 8px 0; }
li { margin: 4px 0; color: #374151; }
li strong { color: %(dark)s; }

/* ── Tables ── */
table {
  border-collapse: collapse;
  width: 100%%;
  margin: 12px 0;
  font-size: 11px;
}
thead tr { background: %(dark)s; }
th {
  color: white;
  padding: 8px 12px;
  text-align: left;
  font-weight: 600;
  font-size: 10px;
  letter-spacing: 0.3px;
}
td {
  border: 1px solid #E2E8F0;
  padding: 7px 12px;
  color: #374151;
}
tr:nth-child(even) td { background: %(light)s; }

/* ── Divider ── */
hr {
  border: none;
  border-top: 1px solid #E2E8F0;
  margin: 24px 0;
}

/* ── Page-break control ── */
h1, h2, h3 { page-break-after: avoid; }
.tool-card, table, blockquote { page-break-inside: avoid; }

/* ── Evidence section ── */
.evidence-section {
  margin-top: 36px;
}
.evidence-section > h2 {
  color: %(gray)s;
  border-left-color: %(gray)s;
  font-size: 12px;
}
.tool-card {
  background: %(light)s;
  border: 1px solid #E2E8F0;
  border-radius: 8px;
  margin: 10px 0;
  overflow: hidden;
}
.tool-card-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 14px;
  background: #F1F5F9;
  border-bottom: 1px solid #E2E8F0;
}
.tool-name {
  font-family: 'JetBrains Mono', monospace;
  font-size: 10px;
  font-weight: 600;
  color: %(dark)s;
  word-break: break-all;
}
.tool-status {
  font-size: 9px;
  font-weight: 600;
  padding: 2px 8px;
  border-radius: 10px;
  text-transform: uppercase;
  letter-spacing: 0.5px;
  white-space: nowrap;
  margin-left: 8px;
}
.tool-status.success { background: #DCFCE7; color: #166534; }
.tool-status.error   { background: #FEE2E2; color: #991B1B; }
.tool-status.no_data { background: #FEF9C3; color: #854D0E; }
.tool-card pre {
  margin: 0;
  border-radius: 0;
  border-left: none;
  font-size: 9.5px;
}

/* ── Footer (last page) ── */
.footer {
  margin-top: 40px;
  padding-top: 16px;
  border-top: 1px solid #E2E8F0;
  display: flex;
  justify-content: space-between;
  align-items: center;
  font-size: 9px;
  color: #94A3B8;
}
.footer-brand { font-weight: 600; color: %(orange)s; }
"""


def generate_pdf(
    title: str,
    analysis: str,
    source: str = "",
    severity: str = "info",
    tool_outputs: list | None = None,
    meta: dict | None = None,
    output_path: str | None = None,
) -> str | None:
    """
    Generate a branded PDF RCA report.
    Returns path to generated PDF, or None on failure.
    """
    try:
        import markdown
        from weasyprint import HTML
    except ImportError as e:
        log.warning(f"PDF unavailable: {e}. pip install weasyprint markdown")
        return None

    try:
        sev = severity.lower() if severity else "info"
        colors = SEVERITY_COLORS.get(sev, SEVERITY_COLORS["unknown"])

        now = datetime.now(tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
        sev_label = _severity_label(sev)

        # Build CSS with dynamic colors
        css = CSS % {
            "dark":       NY_DARK,
            "orange":     NY_ORANGE,
            "gray":       NY_GRAY,
            "light":      NY_LIGHT,
            "code_bg":    CODE_BG,
            "code_fg":    CODE_FG,
            "sev_bg":     colors["bg"],
            "sev_light":  colors["light"],
            "sev_border": colors["border"],
        }

        # Parse analysis markdown — fenced_code + tables + sane_lists
        body_html = markdown.markdown(
            analysis or "",
            extensions=["fenced_code", "tables", "nl2br", "sane_lists"],
        )

        # Cover meta pills
        meta = meta or {}
        meta_items = _build_meta_items(meta, source)

        # Evidence appendix
        evidence_html = _build_evidence(tool_outputs or [])

        # Footer stats
        footer_stats = _build_footer(meta, now)

        html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<style>{css}</style>
</head>
<body>

<!-- ── Full-page Cover ── -->
<div class="cover">
  <div class="cover-circle-mid"></div>

  <div class="cover-top">
    <div class="brand">⚡ Vishwakarma · SRE Intelligence</div>
    <div class="generated-at">{now}</div>
  </div>

  <div class="cover-body">
    <div class="severity-badge">{sev_label}</div>
    <div class="cover-title">{_escape(title)}</div>
    <div class="cover-divider"></div>
    <div class="cover-meta">
      {meta_items}
    </div>
  </div>

  <div style="font-size:10px; color:rgba(255,255,255,0.3); letter-spacing:0.5px;">
    {_source_banner(source, sev)}
  </div>
</div>

<!-- ── Body ── -->
<div class="body">
  {body_html}
  {evidence_html}
  <div class="footer">
    <div><span class="footer-brand">Vishwakarma</span> · SRE Intelligence · Auto-generated RCA</div>
    <div>{footer_stats}</div>
  </div>
</div>

</body>
</html>"""

        if output_path is None:
            fd, output_path = tempfile.mkstemp(suffix=".pdf", prefix="vk_rca_")
            os.close(fd)

        HTML(string=html).write_pdf(output_path)
        log.info(f"PDF written to {output_path}")
        return output_path

    except Exception as e:
        log.error(f"PDF generation failed: {e}", exc_info=True)
        return None


# ── Internal helpers ───────────────────────────────────────────────────────────

def _build_meta_items(meta: dict, source: str) -> str:
    items = []
    if source:
        items.append(f'<div class="meta-item"><strong>Source</strong>{_escape(source.upper())}</div>')
    if meta.get("model"):
        items.append(f'<div class="meta-item"><strong>Model</strong>{_escape(meta["model"])}</div>')
    if meta.get("steps_taken"):
        items.append(f'<div class="meta-item"><strong>Steps</strong>{meta["steps_taken"]}</div>')
    if meta.get("duration_seconds"):
        dur = meta["duration_seconds"]
        items.append(f'<div class="meta-item"><strong>Duration</strong>{dur}s</div>')
    if meta.get("total_cost"):
        items.append(f'<div class="meta-item"><strong>Cost</strong>${meta["total_cost"]:.4f}</div>')
    total_tokens = (meta.get("prompt_tokens") or 0) + (meta.get("completion_tokens") or 0)
    if total_tokens:
        items.append(f'<div class="meta-item"><strong>Tokens</strong>{total_tokens:,}</div>')
    return "\n    ".join(items)


def _build_evidence(tool_outputs: list) -> str:
    if not tool_outputs:
        return ""

    cards = []
    for out in tool_outputs:
        inv = _escape(out.get("invocation", "tool"))
        status = out.get("status", "unknown")
        # status may be a ToolStatus enum value or plain string
        if hasattr(status, "value"):
            status = status.value
        content = out.get("output") or out.get("error") or ""
        if not content:
            continue

        # Head + tail: preserve both context and trailing errors
        content_str = str(content)
        if len(content_str) > 2000:
            head = content_str[:1400]
            tail = content_str[-500:]
            omitted = len(content_str) - 1900
            content_str = head + f"\n\n… [{omitted} chars omitted] …\n\n" + tail

        status_class = str(status).lower().replace(" ", "_")
        cards.append(f"""
<div class="tool-card">
  <div class="tool-card-header">
    <span class="tool-name">{inv}</span>
    <span class="tool-status {status_class}">{status}</span>
  </div>
  <pre><code>{_escape(content_str)}</code></pre>
</div>""")

    if not cards:
        return ""

    return f"""
<div class="evidence-section">
  <h2>🔧 Tool Evidence ({len(cards)} calls)</h2>
  {"".join(cards)}
</div>"""


def _build_footer(meta: dict, now: str) -> str:
    parts = []
    if meta.get("model"):
        parts.append(meta["model"])
    if meta.get("total_cost"):
        parts.append(f"${meta['total_cost']:.4f}")
    parts.append(now)
    return " · ".join(parts)


def _source_banner(source: str, severity: str) -> str:
    emoji = {
        "alertmanager": "🔔",
        "slack": "💬",
        "pagerduty": "📟",
        "jira": "🎫",
        "opsgenie": "🚨",
        "github": "🐙",
    }.get(source.lower() if source else "", "📋")
    src = source.upper() if source else "MANUAL"
    sev = severity.upper() if severity else ""
    return f"{emoji} {src} &nbsp;·&nbsp; {sev}"


def _severity_label(sev: str) -> str:
    return {
        "critical": "🔴 CRITICAL INCIDENT",
        "high":     "🟠 HIGH SEVERITY",
        "medium":   "🟡 MEDIUM SEVERITY",
        "low":      "🟢 LOW SEVERITY",
        "info":     "🔵 INFORMATIONAL",
    }.get(sev, "⚪ INCIDENT RCA")


def _escape(text: str) -> str:
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )
