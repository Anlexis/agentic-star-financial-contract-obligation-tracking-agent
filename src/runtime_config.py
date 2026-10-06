"""AgentCore Platform v1.0"""

# FIN-C2-077 — runtime configuration loader.
#
# The manifest (`config/agent.yaml`) is the static registry entry: id, class,
# trust level, declared secrets and extras. Runtime parameters live in
# `config/config.yaml` and are handed to the graph as `Graph(config=...)`.
#
# Two callers load it:
#   * the platform — AgentRegistry reads config/config.yaml and constructs the
#     agent with it;
#   * `src/api/server.py` — the standalone entry point, which used to construct
#     `Graph()` with NO config at all. Every declared runtime value (max_retry,
#     the llm block) was therefore dead on the standalone path while looking
#     configured in the manifest.
#
# `max_retry` is read by the framework backbone's router; the `llm` block is
# forwarded to the inner graph and seeded into inner state (see
# src/graph/graph.py). `security.s3_gate_enabled` is asserted at compile time —
# see FinancialContractReviewAgent._validate_config.

from __future__ import annotations

import logging
import os
from typing import Any, Dict, Optional

logger = logging.getLogger(__name__)

# <repo root>/config/config.yaml — this file lives at <repo root>/src/.
CONFIG_PATH = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "config",
    "config.yaml",
)

_CACHE: Optional[Dict[str, Any]] = None


def load_runtime_config(path: Optional[str] = None) -> Dict[str, Any]:
    """Return the runtime parameters declared in config/config.yaml.

    A missing or unreadable file yields ``{}`` so the agent still constructs
    with framework defaults; the failure is logged rather than swallowed
    silently. PyYAML is imported lazily — it is a framework runtime dependency,
    not a hard module-load coupling for this template.
    """
    global _CACHE
    if path is None and _CACHE is not None:
        return dict(_CACHE)

    target = path or CONFIG_PATH
    loaded: Dict[str, Any] = {}
    try:
        import yaml

        with open(target, "r", encoding="utf-8") as handle:
            parsed = yaml.safe_load(handle) or {}
        if isinstance(parsed, dict):
            loaded = parsed
        else:
            logger.warning("runtime config at %s is not a mapping; ignoring", target)
    except FileNotFoundError:
        logger.warning("runtime config not found at %s; using framework defaults", target)
    except Exception as exc:  # noqa: BLE001 - never let config loading break start-up
        logger.warning("runtime config at %s could not be read (%s)", target, exc)

    if path is None:
        _CACHE = loaded
    return dict(loaded)


def reset_cache() -> None:
    """Drop the cached config (tests that write a temporary config.yaml)."""
    global _CACHE
    _CACHE = None
