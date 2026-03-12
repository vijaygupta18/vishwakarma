"""
Toolset manager — discovers, loads, and manages all toolsets.

Loads from:
  1. Built-in YAML toolsets (vishwakarma/plugins/toolsets/*.yaml)
  2. Built-in Python toolsets (auto-discovered via registry)
  3. Custom toolsets from config (yaml paths or Python classes)

Caches toolset health status to avoid re-pinging services on every request.
"""
import importlib
import logging
import os
from pathlib import Path
from typing import Any

from vishwakarma.core.tools import Toolset, ToolsetHealth, YAMLToolset
from vishwakarma.utils.cache import TTLCache

log = logging.getLogger(__name__)

# Registry of Python toolset classes — populated by each toolset module
_PYTHON_TOOLSET_REGISTRY: dict[str, type[Toolset]] = {}

# Status cache: avoid re-checking connectivity on every request (5min TTL)
_status_cache = TTLCache(ttl_seconds=300)

BUILTIN_TOOLSETS_DIR = Path(__file__).parent.parent / "plugins" / "toolsets"


def register_toolset(cls: type[Toolset]) -> type[Toolset]:
    """Decorator to register a Python toolset class."""
    _PYTHON_TOOLSET_REGISTRY[cls.name] = cls
    return cls


class ToolsetManager:
    """
    Manages all toolsets for a Vishwakarma instance.
    """

    def __init__(
        self,
        toolsets_config: dict[str, dict[str, Any]] | None = None,
        custom_toolset_paths: list[str] | None = None,
        mcp_servers: dict[str, dict] | None = None,
    ):
        self._config = toolsets_config or {}
        self._custom_paths = custom_toolset_paths or []
        self._mcp = mcp_servers or {}
        self._loaded: list[Toolset] = []
        self._load_all()

    def _load_all(self):
        """Load all toolsets: built-in YAML + Python + custom."""
        # 1. Load built-in YAML toolsets
        self._load_yaml_dir(BUILTIN_TOOLSETS_DIR)

        # 2. Load built-in Python toolsets (auto-discovered via imports)
        self._import_python_toolsets()
        for name, cls in _PYTHON_TOOLSET_REGISTRY.items():
            cfg = self._get_config(name)
            enabled = cfg.get("enabled", True)
            if not enabled:
                log.debug(f"Toolset {name} disabled in config")
                continue
            try:
                instance = cls(config=cfg.get("config", {}))
                self._loaded.append(instance)
            except Exception as e:
                log.warning(f"Failed to load Python toolset {name}: {e}")

        # 3. Load custom YAML toolsets
        for path in self._custom_paths:
            self._load_yaml_file(path)

        log.info(f"Loaded {len(self._loaded)} toolsets")

    def _load_yaml_dir(self, directory: Path):
        if not directory.exists():
            return
        for yaml_file in sorted(directory.glob("*.yaml")):
            self._load_yaml_file(str(yaml_file))

    def _load_yaml_file(self, path: str):
        try:
            ts = YAMLToolset(path, config=self._get_config_for_yaml(path))
            cfg = self._get_config(ts.name)
            ts.enabled = cfg.get("enabled", ts.enabled)
            if ts.enabled:
                self._loaded.append(ts)
        except Exception as e:
            log.warning(f"Failed to load YAML toolset {path}: {e}")

    def _get_config(self, name: str) -> dict:
        return self._config.get(name, {})

    def _get_config_for_yaml(self, path: str) -> dict:
        # Try to find config by toolset name after loading
        import yaml
        try:
            with open(path) as f:
                spec = yaml.safe_load(f)
            name = spec.get("name", "")
            return self._get_config(name)
        except Exception:
            return {}

    def _import_python_toolsets(self):
        """Import all Python toolset modules so they register themselves."""
        toolsets_pkg = "vishwakarma.plugins.toolsets"
        toolsets_dir = Path(__file__).parent.parent / "plugins" / "toolsets"

        for item in sorted(toolsets_dir.iterdir()):
            if item.is_dir() and (item / "__init__.py").exists():
                module_name = f"{toolsets_pkg}.{item.name}"
            elif item.suffix == ".py" and item.name != "__init__.py":
                module_name = f"{toolsets_pkg}.{item.stem}"
            else:
                continue

            try:
                importlib.import_module(module_name)
            except ImportError as e:
                log.debug(f"Could not import toolset module {module_name}: {e}")

    def active_toolsets(self) -> list[Toolset]:
        """Return toolsets that are enabled and healthy."""
        return [
            ts for ts in self._loaded
            if ts.enabled and ts.health != ToolsetHealth.FAILED
        ]

    def all_toolsets(self) -> list[Toolset]:
        return self._loaded

    def check_all(self, force: bool = False) -> dict[str, ToolsetHealth]:
        """
        Run prerequisites checks on all toolsets.
        Results are cached for 5 minutes unless force=True.
        """
        results = {}
        for ts in self._loaded:
            if not force:
                cached = _status_cache.get(ts.name)
                if cached is not None:
                    ts._health = cached
                    results[ts.name] = cached
                    continue

            health = ts.run_prerequisites()
            _status_cache.set(ts.name, health)
            results[ts.name] = health
            if health == ToolsetHealth.FAILED:
                log.warning(f"Toolset {ts.name} failed prerequisites: {ts.error}")
            else:
                log.debug(f"Toolset {ts.name}: {health.value}")

        return results

    def get(self, name: str) -> Toolset | None:
        for ts in self._loaded:
            if ts.name == name:
                return ts
        return None
