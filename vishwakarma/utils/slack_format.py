"""
Utilities for formatting text for Slack mrkdwn.

Conversions:
  ## Header    → *Header*
  **bold**     → *bold*
  _italic_     → _italic_  (unchanged)
  *italic*     → _italic_
  ~~strike~~   → ~strike~
  - bullet     → • bullet
  [text](url)  → <url|text>
  ```code```   → ```code``` (lang tag stripped)
  | tables |   → plain text rows
  ---          → ─────────────────────
"""
import re


def md_to_slack(text: str) -> str:
    """Convert markdown to Slack mrkdwn format."""
    # Normalise line endings
    text = text.replace('\r\n', '\n').replace('\r', '\n')

    lines = text.split('\n')
    result: list[str] = []
    in_code_block = False
    table_buffer: list[str] = []

    def inline_fmt(s: str) -> str:
        """Apply inline bold/italic/strike to a string."""
        s = re.sub(r'\*\*(.+?)\*\*', r'*\1*', s)
        s = re.sub(r'~~(.+?)~~', r'~\1~', s)
        return s

    def flush_table():
        """Convert buffered markdown table lines to plain text."""
        if not table_buffer:
            return
        rows = []
        for tl in table_buffer:
            # Skip separator rows (---|---|---)
            if re.match(r'^[\s|:-]+$', tl):
                continue
            cells = [inline_fmt(c.strip()) for c in tl.strip().strip('|').split('|')]
            rows.append('  '.join(cells))
        if rows:
            # First row = header — bold it
            result.append(f'*{rows[0]}*')
            for r in rows[1:]:
                result.append(f'  {r}')
        table_buffer.clear()

    for line in lines:
        line = line.rstrip()

        # ── Fenced code blocks ────────────────────────────────────────────────
        if re.match(r'^```', line):
            if table_buffer:
                flush_table()
            in_code_block = not in_code_block
            # Strip language tag (```python → ```)
            result.append('```' if in_code_block else '```')
            continue

        if in_code_block:
            result.append(line)
            continue

        # ── Markdown tables (lines starting with |) ───────────────────────────
        if line.startswith('|'):
            table_buffer.append(line)
            continue
        else:
            if table_buffer:
                flush_table()

        # ── Headers: #, ##, ### → *bold* (allow leading whitespace) ────────────
        m = re.match(r'^\s*(#{1,6})\s+(.+)', line)
        if m:
            level = len(m.group(1))
            header = m.group(2).strip()
            # Add blank line before H1/H2 for visual separation
            if level <= 2 and result and result[-1] != '':
                result.append('')
            result.append(f'*{header}*')
            continue

        # ── Horizontal rules ──────────────────────────────────────────────────
        if re.match(r'^\s*[-*_]{3,}\s*$', line):
            result.append('─────────────────────')
            continue

        # ── Bullet lists: - or * at line start → • ───────────────────────────
        line = re.sub(r'^(\s*)[-*]\s+', r'\1• ', line)

        # ── Italic FIRST: *text* (single, not adjacent to *) → _text_ ────────
        # Must run before bold so **bold** double-stars don't get caught here
        line = re.sub(r'(?<!\*)\*(?!\*)([^*\n]+?)(?<!\*)\*(?!\*)', r'_\1_', line)

        # ── Bold: **text** → *text* ───────────────────────────────────────────
        line = re.sub(r'\*\*(.+?)\*\*', r'*\1*', line)

        # ── Strikethrough: ~~text~~ → ~text~ ─────────────────────────────────
        line = re.sub(r'~~(.+?)~~', r'~\1~', line)

        # ── Links: [text](url) → <url|text> ──────────────────────────────────
        line = re.sub(r'\[([^\]]+)\]\(([^)]+)\)', r'<\2|\1>', line)

        # ── Blockquotes: > text → › text ─────────────────────────────────────
        line = re.sub(r'^>\s+', '› ', line)

        result.append(line)

    # Flush any trailing table
    if table_buffer:
        flush_table()

    # Clean up excessive blank lines (max 1 consecutive blank)
    cleaned: list[str] = []
    prev_blank = False
    for line in result:
        is_blank = line.strip() == ''
        if is_blank and prev_blank:
            continue
        cleaned.append(line)
        prev_blank = is_blank

    return '\n'.join(cleaned).strip()


def chunk_for_slack(text: str, max_len: int = 2900) -> list[str]:
    """
    Split text into Slack-safe chunks (<= max_len chars).
    Splits at paragraph (double newline) boundaries where possible,
    falling back to line boundaries, then hard split as last resort.
    """
    if len(text) <= max_len:
        return [text] if text.strip() else []

    chunks: list[str] = []
    current = ""

    for para in re.split(r'\n{2,}', text):
        para = para.strip()
        if not para:
            continue

        candidate = f"{current}\n\n{para}" if current else para

        if len(candidate) <= max_len:
            current = candidate
        else:
            if current:
                chunks.append(current.strip())
            # Paragraph itself too long — split at line boundaries
            if len(para) > max_len:
                sub_current = ""
                for line in para.split('\n'):
                    sub_candidate = f"{sub_current}\n{line}" if sub_current else line
                    if len(sub_candidate) <= max_len:
                        sub_current = sub_candidate
                    else:
                        if sub_current:
                            chunks.append(sub_current.strip())
                        # Single line too long — hard split
                        while len(line) > max_len:
                            chunks.append(line[:max_len])
                            line = line[max_len:]
                        sub_current = line
                if sub_current:
                    current = sub_current
            else:
                current = para

    if current.strip():
        chunks.append(current.strip())

    return [c for c in chunks if c]
