"""Load configuration and secrets from Azure Key Vault into the environment.

Local development uses a plain ``.env`` and never sets ``AZURE_KEY_VAULT_URL``,
so this module is a no-op.

In AKS, secrets are injected by CSI (SecretProviderClass) as environment
variables. Do **not** point this loader at the shared ecommerce Key Vault
without ``KEY_VAULT_SECRET_PREFIX`` — that vault holds Magento and other app
secrets, and listing them all would dump them into this process.

Optional in-process load (dedicated vault or prefixed names):
    AZURE_KEY_VAULT_URL or AZURE_KEY_VAULT_NAME
    KEY_VAULT_SECRET_PREFIX  - only secrets whose names start with this
                               (e.g. FINOPS-). The prefix is stripped before
                               mapping to env (FINOPS-DATABASE-URL → DATABASE_URL).
    KEY_VAULT_REQUIRED       - fail startup if the vault cannot be read
    KEY_VAULT_OVERRIDE       - default true; Key Vault wins over existing env
    APP_CONFIG_SECRET        - secret whose value is the full config.yaml
"""
from __future__ import annotations

import logging
import os

log = logging.getLogger(__name__)

_LOADED = False

_TRUE = {"1", "true", "yes", "on", "y", "t"}


def _truthy(value: str | None, default: bool = False) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in _TRUE


def vault_url() -> str | None:
    url = os.environ.get("AZURE_KEY_VAULT_URL")
    if url:
        return url.rstrip("/") + "/"
    name = os.environ.get("AZURE_KEY_VAULT_NAME")
    if name:
        return f"https://{name}.vault.azure.net/"
    return None


def secret_name_to_env(name: str, prefix: str = "") -> str:
    trimmed = name
    if prefix and trimmed.startswith(prefix):
        trimmed = trimmed[len(prefix) :]
    return trimmed.upper().replace("-", "_")


def load_secrets_into_env(*, force: bool = False) -> bool:
    """Pull matching secrets from Key Vault into ``os.environ``.

    Idempotent: only runs once per process unless ``force`` is set. Returns True
    if Key Vault was queried and at least attempted, else False (not configured).
    """
    global _LOADED
    if _LOADED and not force:
        return False

    url = vault_url()
    if not url:
        _LOADED = True
        return False

    required = _truthy(os.environ.get("KEY_VAULT_REQUIRED"))
    override = _truthy(os.environ.get("KEY_VAULT_OVERRIDE"), default=True)
    config_secret = os.environ.get("APP_CONFIG_SECRET", "app-config")
    prefix = os.environ.get("KEY_VAULT_SECRET_PREFIX", "").strip()

    try:
        from azure.identity import DefaultAzureCredential
        from azure.keyvault.secrets import SecretClient
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        msg = f"Key Vault libraries not installed ({exc})"
        if required:
            raise RuntimeError(msg) from exc
        log.warning("%s; skipping Key Vault load", msg)
        _LOADED = True
        return False

    try:
        credential = DefaultAzureCredential(exclude_interactive_browser_credential=True)
        client = SecretClient(vault_url=url, credential=credential)

        loaded = 0
        for prop in client.list_properties_of_secrets():
            if prop.enabled is False:
                continue
            name = prop.name
            if prefix and not name.startswith(prefix) and name != config_secret:
                continue
            value = client.get_secret(name).value
            if value is None:
                continue
            if name == config_secret:
                os.environ["APP_CONFIG_YAML"] = value
                loaded += 1
                continue
            env_key = secret_name_to_env(name, prefix)
            if env_key in os.environ and not override:
                continue
            os.environ[env_key] = value
            loaded += 1

        log.info("Key Vault: loaded %d secret(s) from %s", loaded, url)
        _LOADED = True
        return True
    except Exception:  # noqa: BLE001 - never let secret loading crash startup
        if required:
            raise
        log.exception("Key Vault: failed to load from %s; continuing", url)
        _LOADED = True
        return False
