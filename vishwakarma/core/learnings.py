"""
Learnings manager — persistent, categorized knowledge built from past incidents.
Stored at /data/learnings/{category}.md on PVC.
"""
import os
import re
import logging
from datetime import datetime

log = logging.getLogger(__name__)

# Default categories created on first run
_DEFAULT_CATEGORIES = ["rds", "redis", "drainer", "kubernetes", "networking", "general"]

# Alert label/name → category keyword mapping
_ALERT_CATEGORY_KEYWORDS: dict[str, list[str]] = {
    "rds": ["rds", "aurora", "database", "db", "cpu", "sql", "replication", "connection", "query", "performance"],
    "redis": ["redis", "cache", "elasticache", "evict", "memory"],
    "drainer": ["drainer", "drain"],
    "kubernetes": ["pod", "deploy", "node", "oom", "crash", "restart", "evict", "pvc", "k8s", "container", "allocator", "alloc", "producer", "drainer"],
    "networking": ["alb", "5xx", "ingress", "network", "dns", "istio", "latency", "timeout", "cmrl", "cris", "pt", "transit"],
}

_VALID_CATEGORY_RE = re.compile(r'^[a-z0-9][a-z0-9_-]{0,63}$')


def _valid_category_name(name: str) -> bool:
    return bool(_VALID_CATEGORY_RE.match(name))


class LearningsManager:
    """
    Manages persistent, categorized learnings derived from past incidents.
    Each category is stored as a Markdown file under `path/`.
    Categories are dynamic — any valid name can be created at runtime.
    """

    def __init__(self, path: str = "/data/learnings"):
        self.path = path
        os.makedirs(path, exist_ok=True)
        self._init_defaults()

    # ── Internal helpers ──────────────────────────────────────────────────────

    def _file(self, category: str) -> str:
        return os.path.join(self.path, f"{category}.md")

    def _init_defaults(self) -> None:
        """Create default category files with a header if they don't exist yet."""
        for cat in _DEFAULT_CATEGORIES:
            fpath = self._file(cat)
            if not os.path.exists(fpath):
                try:
                    with open(fpath, "w", encoding="utf-8") as f:
                        f.write(f"# {cat.capitalize()} Learnings\n")
                except OSError as e:
                    log.warning(f"Could not initialise learnings file {fpath}: {e}")

    def _all_categories(self) -> list[str]:
        """Return all category names by scanning the directory."""
        try:
            names = []
            for fname in sorted(os.listdir(self.path)):
                if fname.endswith(".md"):
                    names.append(fname[:-3])
            return names
        except OSError:
            return list(_DEFAULT_CATEGORIES)

    # ── Public API ────────────────────────────────────────────────────────────

    def create(self, category: str) -> None:
        """Create a new category file. Raises ValueError for invalid names."""
        cat = category.lower().strip()
        if not _valid_category_name(cat):
            raise ValueError(
                f"Invalid category name '{cat}'. "
                "Use lowercase letters, digits, hyphens, or underscores (max 64 chars)."
            )
        fpath = self._file(cat)
        if not os.path.exists(fpath):
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(f"# {cat.capitalize()} Learnings\n")

    def get(self, category: str) -> str:
        """Return the full content of a category file."""
        cat = category.lower().strip()
        fpath = self._file(cat)
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return f"# {cat.capitalize()} Learnings\n"
        except OSError as e:
            log.error(f"Could not read learnings file {fpath}: {e}")
            return ""

    def set(self, category: str, content: str) -> None:
        """Overwrite the content of a category file."""
        cat = category.lower().strip()
        fpath = self._file(cat)
        try:
            with open(fpath, "w", encoding="utf-8") as f:
                f.write(content)
        except OSError as e:
            log.error(f"Could not write learnings file {fpath}: {e}")
            raise

    def append(self, category: str, fact: str) -> None:
        """Append a bullet-point fact to a category file."""
        cat = category.lower().strip()
        fpath = self._file(cat)
        line = f"- {fact.strip()}\n"
        try:
            with open(fpath, "a", encoding="utf-8") as f:
                f.write(line)
        except OSError as e:
            log.error(f"Could not append to learnings file {fpath}: {e}")
            raise

    def forget(self, category: str, keyword: str) -> int:
        """
        Remove all lines containing `keyword` (case-insensitive) from a category file.
        Returns the number of lines removed.
        """
        cat = category.lower().strip()
        fpath = self._file(cat)
        keyword_lower = keyword.lower()
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                lines = f.readlines()
        except FileNotFoundError:
            return 0
        except OSError as e:
            log.error(f"Could not read learnings file {fpath}: {e}")
            return 0

        kept = [l for l in lines if keyword_lower not in l.lower()]
        removed = len(lines) - len(kept)
        if removed:
            try:
                with open(fpath, "w", encoding="utf-8") as f:
                    f.writelines(kept)
            except OSError as e:
                log.error(f"Could not write learnings file {fpath}: {e}")
                return 0
        return removed

    def list_categories(self) -> list[dict]:
        """
        Return metadata for all categories (scanned from disk):
        [{category, fact_count, size_bytes, last_modified}]
        """
        result = []
        for cat in self._all_categories():
            fpath = self._file(cat)
            try:
                stat = os.stat(fpath)
                size_bytes = stat.st_size
                last_modified = datetime.fromtimestamp(stat.st_mtime).isoformat()
                with open(fpath, "r", encoding="utf-8") as f:
                    lines = f.readlines()
                fact_count = sum(1 for l in lines if l.strip().startswith("- "))
            except FileNotFoundError:
                size_bytes = 0
                last_modified = None
                fact_count = 0
            except OSError as e:
                log.warning(f"Could not stat {fpath}: {e}")
                size_bytes = 0
                last_modified = None
                fact_count = 0
            result.append({
                "category": cat,
                "fact_count": fact_count,
                "size_bytes": size_bytes,
                "last_modified": last_modified,
            })
        return result

    def compact(self, category: str, llm_summarize_fn) -> bool:
        """
        If a category has grown large (>30 facts or >4KB), use the LLM to
        consolidate it into the best, non-redundant set of facts.
        Returns True if compaction happened.
        """
        cat = category.lower().strip()
        content = self.get(cat)
        facts = [l.strip() for l in content.splitlines() if l.strip().startswith("- ")]

        if len(facts) <= 50 and len(content) <= 5000:
            return False

        log.info(f"Compacting learnings category '{cat}' ({len(facts)} facts, {len(content)} bytes)")

        prompt = (
            f"The following is a list of learned facts for incident category '{cat}'.\n"
            f"Consolidate them into the best, most actionable set of facts:\n"
            f"- Merge duplicates and near-duplicates into one\n"
            f"- Keep facts specific (service names, error types, fix patterns)\n"
            f"- Remove vague or generic facts\n"
            f"- Output ONLY bullet points starting with '- '\n\n"
            f"{content}"
        )
        try:
            compacted = llm_summarize_fn(prompt)
            # Rebuild file: header + compacted facts
            header = f"# {cat.capitalize()} Learnings\n"
            self.set(cat, header + compacted.strip() + "\n")
            log.info(f"Compacted '{cat}' learnings successfully")
            return True
        except Exception as e:
            log.warning(f"Learnings compaction failed for '{cat}': {e}")
            return False

    def for_alert(self, alert_name: str) -> str:
        """
        Map an alert name to relevant categories using keyword matching,
        then return only the bullet-point fact lines from those categories,
        prefixed with a section header.

        Returns an empty string if no relevant facts are found.
        """
        alert_lower = alert_name.lower()
        matched_categories: list[str] = []

        for cat, keywords in _ALERT_CATEGORY_KEYWORDS.items():
            if any(kw in alert_lower for kw in keywords):
                matched_categories.append(cat)

        # Always include general
        if "general" not in matched_categories:
            matched_categories.append("general")

        parts: list[str] = []
        for cat in matched_categories:
            fpath = self._file(cat)
            try:
                with open(fpath, "r", encoding="utf-8") as f:
                    lines = f.readlines()
            except (FileNotFoundError, OSError):
                continue

            facts = [l.rstrip() for l in lines if l.strip().startswith("- ")]
            if facts:
                header = f"## Learned Facts ({cat})"
                parts.append(header + "\n" + "\n".join(facts))

        return "\n\n".join(parts)
