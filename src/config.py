"""Config loader with ${ENV_VAR} expansion."""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

import yaml

# Supports ${VAR} and ${VAR:-default}.
_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)(?::-([^}]*))?\}")


def _sub(match: re.Match) -> str:
    var, default = match.group(1), match.group(2)
    return os.environ.get(var) or (default or "")


def _expand(value: Any) -> Any:
    if isinstance(value, str):
        return _ENV_PATTERN.sub(_sub, value)
    if isinstance(value, dict):
        return {k: _expand(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand(v) for v in value]
    return value


def load_config(path: str | None = None) -> dict[str, Any]:
    # Pull secrets (and optionally the full config) from Azure Key Vault when
    # AZURE_KEY_VAULT_URL/NAME is set. No-op otherwise. Runs once per process.
    from .keyvault import load_secrets_into_env

    load_secrets_into_env()

    raw_yaml = os.environ.get("APP_CONFIG_YAML")
    if raw_yaml:
        raw = yaml.safe_load(raw_yaml)
    else:
        cfg_path = Path(path or os.environ.get("CONFIG_PATH", "config/config.yaml"))
        with cfg_path.open("r", encoding="utf-8") as fh:
            raw = yaml.safe_load(fh)
    return _expand(raw)
