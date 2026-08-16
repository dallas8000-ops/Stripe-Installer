"""Push vault secrets and/or inline variables to Railway service environment."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from apps.projects.models import Project
from apps.vault.models import get_secret, set_secret, vault_health

STRIPE_ENV_KEYS = [
    "STRIPE_SECRET_KEY",
    "STRIPE_PUBLISHABLE_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY",
    "DATABASE_URL",
]

# Non-secret defaults — override DATABASE_URL / secrets via vault or request `variables`.
# Kistie uses dedicated Railway plugin "Postgres" (see railway_postgres.RAILWAY_POSTGRES_SERVICE_BY_PRESET).
KISTIE_STORE_PRESET: dict[str, str] = {
    "DJANGO_DEBUG": "False",
    "DJANGO_ENABLE_ADMIN": "False",
    "DATABASE_URL": "${{Postgres.DATABASE_URL}}",
    "ALLOWED_HOSTS": ".railway.app kistie-store-production.up.railway.app",
    "CSRF_TRUSTED_ORIGINS": "https://kistie-store-production.up.railway.app",
    "SITE_URL": "https://kistie-store-production.up.railway.app",
    "DJANGO_EMAIL_BACKEND": "django.core.mail.backends.smtp.EmailBackend",
    "EMAIL_HOST": "smtp.gmail.com",
    "EMAIL_PORT": "587",
    "EMAIL_USE_TLS": "True",
    "CONTACT_RECIPIENT_EMAIL": "dallas8000@gmail.com",
    "DJANGO_DEFAULT_FROM_EMAIL": "dallas8000@gmail.com",
    "ORDER_ALERT_EMAIL": "dallas8000@gmail.com",
    "WHATSAPP_STORE_NUMBER": "256704757198",
    "INSTAGRAM_PROFILE_URL": "https://www.instagram.com/kistie_storeug/",
}

KISTIE_STORE_VAULT_KEYS = [
    "DJANGO_SECRET_KEY",
    "DATABASE_URL",
    "EMAIL_HOST_USER",
    "EMAIL_HOST_PASSWORD",
]

# SilverFox — men's Django SSR storefront (separate Postgres from Kistie Store).
SILVERFOX_PRESET: dict[str, str] = {
    "DEBUG": "False",
    "DJANGO_ENABLE_ADMIN": "True",
    "DJANGO_SSL_REDIRECT": "False",
    "DATABASE_URL": "${{Postgres.DATABASE_URL}}",
    "ALLOWED_HOSTS": ".railway.app .up.railway.app silverfox-production.up.railway.app",
    "CSRF_TRUSTED_ORIGINS": "https://silverfox-production.up.railway.app",
}

SILVERFOX_VAULT_KEYS = [
    "DJANGO_SECRET_KEY",
    "DATABASE_URL",
    "STRIPE_SECRET_KEY",
    "STRIPE_PUBLISHABLE_KEY",
    "STRIPE_WEBHOOK_SECRET",
    "EMAIL_HOST_USER",
    "EMAIL_HOST_PASSWORD",
]

# AgriPay — Django uses SECRET_KEY; DATABASE_URL via Railway Postgres reference.
AGRIPAY_PRESET: dict[str, str] = {
    "DEBUG": "False",
    "ALLOWED_HOSTS": ".railway.app .up.railway.app healthcheck.railway.app",
    "APP_URL": "https://agripay-api-production.up.railway.app",
    "DATABASE_URL": "${{Postgres-AgriPay.DATABASE_URL}}",
}

AGRIPAY_VAULT_KEYS = [
    "SECRET_KEY",
    "STRIPE_SECRET_KEY",
    "STRIPE_PUBLISHABLE_KEY",
    "STRIPE_WEBHOOK_SECRET",
]

ENV_PRESETS: dict[str, dict[str, str]] = {
    "kistie-store": KISTIE_STORE_PRESET,
    "silverfox": SILVERFOX_PRESET,
    "agripay-logistics-ai": AGRIPAY_PRESET,
}

PRESET_VAULT_KEYS: dict[str, list[str]] = {
    "kistie-store": KISTIE_STORE_VAULT_KEYS,
    "silverfox": SILVERFOX_VAULT_KEYS,
    "agripay-logistics-ai": AGRIPAY_VAULT_KEYS,
}

VAULT_KEY_ALIASES: dict[str, dict[str, str]] = {
    "agripay-logistics-ai": {"DJANGO_SECRET_KEY": "SECRET_KEY"},
}

from .railway_client import railway_gql as _railway_gql_impl

RAILWAY_GQL = "https://backboard.railway.app/graphql/v2"


def merge_env_vars(
    *,
    preset: dict[str, str] | None = None,
    vault: dict[str, str] | None = None,
    inline: dict[str, str] | None = None,
) -> dict[str, str]:
    """Merge preset → vault → inline (later sources override earlier)."""
    merged: dict[str, str] = {}
    if preset:
        merged.update({k: v for k, v in preset.items() if v is not None and str(v).strip()})
    if vault:
        merged.update({k: v for k, v in vault.items() if v})
    if inline:
        merged.update({k: str(v).strip() for k, v in inline.items() if v is not None and str(v).strip()})
    return merged


def is_railway_reference(value: str) -> bool:
    """True when value is a Railway service reference like ${{Postgres.DATABASE_URL}}."""
    text = str(value or "").strip()
    return text.startswith("${{") and text.endswith("}}")


def is_placeholder_database_url(value: str) -> bool:
    """True for hub .env.example templates and other non-production Postgres URLs."""
    from urllib.parse import urlparse

    text = str(value or "").strip().lower()
    if not text or is_railway_reference(value):
        return not bool(text)
    if not text.startswith(("postgres://", "postgresql://")):
        return True
    parsed = urlparse(text)
    if not (parsed.hostname or "").strip():
        return True
    markers = (
        "localhost",
        "127.0.0.1",
        "yourdb",
        "user:pass@",
        "@host:",
        "host:5432/db",
        "example.com",
    )
    return any(marker in text for marker in markers)


def merge_service_env_vars(
    existing: dict[str, str],
    incoming: dict[str, str],
    *,
    skip_empty_overwrites: bool = True,
    preset: str | None = None,
    slug: str | None = None,
) -> dict[str, str]:
    """Merge env vars before Railway upsert — incoming wins; empty incoming never wipes secrets."""
    from .railway_postgres import database_url_should_be_replaced

    merged = dict(existing)
    for key, value in incoming.items():
        incoming_value = str(value)
        if skip_empty_overwrites and not incoming_value.strip():
            if key in merged and str(merged[key]).strip():
                continue
            continue
        if key == "DATABASE_URL":
            existing_db = str(merged.get("DATABASE_URL", "")).strip()
            if database_url_should_be_replaced(existing_db, incoming_value, preset=preset, slug=slug):
                merged[key] = incoming_value
                continue
            if existing_db.startswith(("postgres://", "postgresql://")) and is_railway_reference(
                incoming_value
            ):
                # Never replace a working dedicated Postgres URL with a plugin reference.
                continue
        merged[key] = incoming_value
    return merged


def _apply_vault_overrides(preset_vars: dict[str, str], vault_vars: dict[str, str]) -> dict[str, str]:
    """Apply vault values, but keep Railway Postgres references from preset when vault has literal URLs."""
    result = dict(vault_vars)
    preset_db = preset_vars.get("DATABASE_URL", "")
    vault_db = vault_vars.get("DATABASE_URL", "")
    if (
        is_railway_reference(preset_db)
        and vault_db.startswith(("postgres://", "postgresql://"))
    ):
        result.pop("DATABASE_URL", None)
    return result


def _railway_gql(token: str, query: str, variables: dict | None = None) -> dict:
    return _railway_gql_impl(token, query, variables)


def _railway_environment_id(token: str, project_id: str) -> str:
    data = _railway_gql(
        token,
        "query($id: String!) { project(id: $id) { environments { edges { node { id name } } } } }",
        {"id": project_id},
    )
    edges = ((data.get("project") or {}).get("environments") or {}).get("edges") or []
    if not edges:
        raise RuntimeError("No environment found for Railway project")

    for edge in edges:
        node = edge.get("node") or {}
        name = str(node.get("name") or "").strip().lower()
        env_id = str(node.get("id") or "").strip()
        if env_id and name == "production":
            return env_id

    env_id = ((edges[0] if edges else {}).get("node") or {}).get("id")
    if not env_id:
        raise RuntimeError("No environment found for Railway project")
    return env_id


def get_railway_env_vars(
    token: str,
    project_id: str,
    service_id: str,
    environment_id: str,
) -> dict[str, str]:
    data = _railway_gql(
        token,
        """
        query($projectId: String!, $environmentId: String!, $serviceId: String!) {
          variables(projectId: $projectId, environmentId: $environmentId, serviceId: $serviceId)
        }
        """,
        {
            "projectId": project_id,
            "environmentId": environment_id,
            "serviceId": service_id,
        },
    )
    variables = data.get("variables") or {}
    if not isinstance(variables, dict):
        return {}
    return {str(key): str(value) for key, value in variables.items()}


def push_to_railway(
    token: str,
    project_id: str,
    service_id: str,
    env_vars: dict[str, str],
    environment_id: str | None = None,
    *,
    preserve_existing: bool = True,
    preset: str | None = None,
) -> dict[str, Any]:
    if not environment_id:
        environment_id = _railway_environment_id(token, project_id)

    upsert_vars = env_vars
    merge_meta: dict[str, Any] = {"mode": "replace"}
    if preserve_existing:
        existing = get_railway_env_vars(token, project_id, service_id, environment_id)
        upsert_vars = merge_service_env_vars(existing, env_vars, preset=preset)
        merge_meta = {
            "mode": "merge",
            "existingKeyCount": len(existing),
            "incomingKeyCount": len(env_vars),
            "mergedKeyCount": len(upsert_vars),
            "preservedKeys": sorted(set(existing) - set(env_vars)),
            "updatedKeys": sorted(
                key for key in env_vars if key in existing and env_vars[key] != existing.get(key)
            ),
            "addedKeys": sorted(set(env_vars) - set(existing)),
        }

    _railway_gql(
        token,
        "mutation($input: VariableCollectionUpsertInput!) { variableCollectionUpsert(input: $input) }",
        {
            "input": {
                "projectId": project_id,
                "serviceId": service_id,
                "environmentId": environment_id,
                "variables": upsert_vars,
            }
        },
    )
    return {
        "pushed": sorted(env_vars.keys()),
        "environmentId": environment_id,
        "merge": merge_meta,
    }


def _vault_subset(project: Project, keys: list[str]) -> dict[str, str]:
    result = {k: v for k in keys if (v := get_secret(project, k))}
    for source, target in VAULT_KEY_ALIASES.get((project.slug or "").strip().lower(), {}).items():
        if target in keys and target not in result and (v := get_secret(project, source)):
            result[target] = v
    if is_placeholder_database_url(result.get("DATABASE_URL", "")):
        result.pop("DATABASE_URL", None)
    return result


def build_env_var_payload(
    project: Project,
    *,
    keys: list[str] | None = None,
    variables: dict[str, str] | None = None,
    preset: str | None = None,
) -> dict[str, str]:
    preset_vars = ENV_PRESETS.get(preset or "", {})
    vault_keys = keys
    if preset and not vault_keys:
        vault_keys = PRESET_VAULT_KEYS.get(preset, [])
    if not vault_keys and not preset_vars and not variables:
        vault_keys = STRIPE_ENV_KEYS
    vault_vars = _vault_subset(project, vault_keys or [])
    if preset_vars and vault_vars:
        vault_vars = _apply_vault_overrides(preset_vars, vault_vars)
    return merge_env_vars(preset=preset_vars or None, vault=vault_vars or None, inline=variables)


def push_vault_env_to_platform(
    project: Project,
    platform: str,
    service_id: str,
    *,
    project_id: str | None = None,
    environment_id: str | None = None,
    keys: list[str] | None = None,
    variables: dict[str, str] | None = None,
    preset: str | None = None,
) -> dict[str, Any]:
    """Push vault secrets, optional preset defaults, and inline variables to Railway."""
    if platform != "railway":
        raise ValueError(f"Unsupported platform '{platform}' — supported: railway")

    token = get_secret(project, "RAILWAY_API_TOKEN")
    if not token:
        raise RuntimeError(
            "RAILWAY_API_TOKEN not in vault — create at https://railway.com/account/tokens"
        )
    if not project_id:
        raise RuntimeError("project_id is required for Railway env push")

    if preset and preset not in ENV_PRESETS:
        raise ValueError(f"Unknown preset '{preset}' — available: {', '.join(sorted(ENV_PRESETS))}")

    inline = None
    if variables:
        if not isinstance(variables, dict):
            raise ValueError("variables must be an object of key/value strings")
        inline = {str(k): str(v) for k, v in variables.items()}

    env_vars = build_env_var_payload(project, keys=keys, variables=inline, preset=preset)
    if not env_vars:
        health = vault_health(project)
        if health["unreadableCount"]:
            raise RuntimeError(
                f"Cannot push env vars: {health['unreadableCount']} vault secret(s) cannot be "
                "decrypted. Restore ~/.stripe-installer/projects/ backup or re-enter keys in the vault."
            )
        return {"pushed": [], "message": "No variables to push (empty preset, vault, and inline)"}

    result = push_to_railway(token, project_id, service_id, env_vars, environment_id, preset=preset)
    db_pushed = env_vars.get("DATABASE_URL")
    if db_pushed and is_railway_reference(db_pushed):
        vault_db = get_secret(project, "DATABASE_URL")
        if not vault_db or is_placeholder_database_url(vault_db):
            set_secret(project, "DATABASE_URL", db_pushed)
    result["preset"] = preset
    result["message"] = (
        f"Pushed {len(env_vars)} var(s) to {platform}: {', '.join(sorted(env_vars.keys()))}"
    )
    return result


def _redis_reference_for_project(token: str, project_id: str) -> str | None:
    """Return ${{ServiceName.REDIS_URL}} for the first Redis plugin in a Railway project."""
    from .railway_resolve import _list_railway_services

    for svc in _list_railway_services(token, project_id):
        name = (svc.get("name") or "").strip()
        if name and "redis" in name.lower():
            return "${{" + name + ".REDIS_URL}}"
    return None


def push_monorepo_railway_live_env(project: Project) -> dict[str, Any] | None:
    """
    Push Railway live URLs to API + web services (Django monorepos).
    API: CLIENT_URL, CSRF, ALLOWED_HOSTS + Stripe keys. Web: VITE_API_URL (redeploy to apply build arg).
  """
    from urllib.parse import urlparse

    from apps.stripe_core.portfolio_catalog import catalog_by_slug, catalog_live_urls
    from apps.stripe_core.hub_keys import get_hub_project, repair_project_vault_from_hub

    entry = catalog_by_slug(project.slug or "")
    live = catalog_live_urls(entry)
    api_url = live.get("apiUrl") or ""
    web_url = live.get("webUrl") or ""
    if not api_url or not web_url or api_url == web_url:
        return None

    token = get_secret(project, "RAILWAY_API_TOKEN")
    hub = get_hub_project(project.owner)
    if not token and hub:
        repair_project_vault_from_hub(project, hub)
        token = get_secret(project, "RAILWAY_API_TOKEN")
    if not token:
        return {"ok": False, "skipped": True, "message": "RAILWAY_API_TOKEN not in vault"}

    from .railway_resolve import resolve_railway_service_by_host

    api_host = (urlparse(api_url).hostname or "").lower()
    web_host = (urlparse(web_url).hostname or "").lower()
    api_project_id, api_service_id = resolve_railway_service_by_host(token, api_host)
    web_project_id, web_service_id = resolve_railway_service_by_host(token, web_host)
    if not api_project_id or not api_service_id:
        return {"ok": False, "message": f"Could not resolve Railway API service for {api_url}"}
    if not web_project_id or not web_service_id:
        return {"ok": False, "message": f"Could not resolve Railway web service for {web_url}"}

    api_hosts = {api_host, web_host, ".railway.app", ".up.railway.app", "healthcheck.railway.app"}
    portfolio_demo = live.get("portfolioDemoUrl") or ""
    if portfolio_demo and portfolio_demo != live.get("demoUrl"):
        demo_host = (urlparse(portfolio_demo).hostname or "").lower()
        if demo_host:
            api_hosts.add(demo_host)

    api_env = build_env_var_payload(project)
    api_env.update(
        {
            "CLIENT_URL": web_url,
            "CSRF_TRUSTED_ORIGINS": ",".join(
                sorted({web_url, api_url, portfolio_demo} - {""})
            ),
            "ALLOWED_HOSTS": ",".join(sorted(h for h in api_hosts if h and not h.startswith("."))),
        }
    )
    if (project.slug or "") == "elite-fintech-systems":
        redis_ref = _redis_reference_for_project(token, api_project_id)
        api_env.update(
            {
                "PLATFORM_TIER": "PLATINUM",
                "DEBUG": "False",
                "DATABASE_URL": "${{elite-fintech-systems-db.DATABASE_URL}}",
            }
        )
        if redis_ref:
            api_env["REDIS_URL"] = redis_ref
    api_result = push_to_railway(token, api_project_id, api_service_id, api_env)

    ws_url = api_url.replace("https://", "wss://").replace("http://", "ws://").rstrip("/") + "/ws/billing/"
    web_env = {
        "VITE_API_URL": api_url,
        "VITE_WS_URL": ws_url,
    }
    web_result = push_to_railway(token, web_project_id, web_service_id, web_env)

    return {
        "ok": True,
        "message": (
            f"Live URLs on Railway — API {api_url}, web {web_url} "
            f"(redeploy web service for VITE_* build args)"
        ),
        "apiUrl": api_url,
        "webUrl": web_url,
        "demoUrl": live.get("demoUrl"),
        "portfolioDemoUrl": live.get("portfolioDemoUrl"),
        "apiServiceId": api_service_id,
        "webServiceId": web_service_id,
        "apiPushed": api_result.get("pushed"),
        "webPushed": web_result.get("pushed"),
    }


def try_auto_push_railway_stripe_env(project: Project) -> dict[str, Any] | None:
    """
    After Stripe webhook provisioning, push STRIPE_* vault vars to the Railway API service.
    Returns push result dict, skip dict with reason, or None when platform is not Railway.
    """
    from pathlib import Path

    from apps.deploy.platform import detect_deploy_platform
    from apps.stripe_core.hub_keys import get_hub_project, repair_project_vault_from_hub

    root = Path(project.local_path or "")
    if not root.is_dir():
        return {"ok": False, "skipped": True, "message": "local_path missing — set real repo folder"}

    scan = project.scan_data or {}
    platform = scan.get("deployPlatform") or detect_deploy_platform(root, project.framework)
    if platform != "railway":
        return None

    hub = get_hub_project(project.owner)
    if hub and not get_secret(project, "RAILWAY_API_TOKEN"):
        repair_project_vault_from_hub(project, hub)

    if not get_secret(project, "RAILWAY_API_TOKEN"):
        return {
            "ok": False,
            "skipped": True,
            "message": "RAILWAY_API_TOKEN not in vault — add on hub project first",
        }

    try:
        from apps.deploy.railway_resolve import preset_for_project

        result = auto_push_railway_env(project, preset=preset_for_project(project))
        monorepo = push_monorepo_railway_live_env(project)
        if monorepo and monorepo.get("ok"):
            result["monorepoLive"] = monorepo
            result["message"] = monorepo.get("message", result.get("message"))
        result["ok"] = True
        return result
    except (RuntimeError, ValueError) as exc:
        return {"ok": False, "message": str(exc)}


def auto_push_railway_env(
    project: Project,
    *,
    preset: str | None = None,
    project_id: str | None = None,
    service_id: str | None = None,
    variables: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Discover Railway targets and push preset + vault env vars (used by deploy pipeline)."""
    from datetime import datetime, timezone

    from .railway_resolve import (
        _list_railway_projects,
        preset_for_project,
        remember_railway_targets,
        resolve_railway_project_id,
        resolve_railway_web_service_id,
        sync_production_url_from_railway,
    )

    token = get_secret(project, "RAILWAY_API_TOKEN")
    if not token:
        raise RuntimeError(
            "RAILWAY_API_TOKEN not in vault — create at https://railway.com/account/tokens"
        )

    resolved_preset = preset or preset_for_project(project)
    resolved_project_id = (project_id or resolve_railway_project_id(project, token) or "").strip()
    if not resolved_project_id:
        listed: list[dict[str, str]] = []
        try:
            listed = _list_railway_projects(token)
        except RuntimeError:
            listed = []
        hint = ""
        if not listed:
            hint = (
                " Your Railway token returned no projects — it may be expired, project-scoped, "
                "or from a different account. In Railway -> Project -> Settings, copy the Project ID "
                "and save RAILWAY_PROJECT_ID in vault (same for the web service's RAILWAY_SERVICE_ID)."
            )
        raise RuntimeError(
            "Could not resolve Railway project ID — add RAILWAY_PROJECT_ID to vault or name the "
            f"Railway project to match this workspace (e.g. silverfox -> SilverFox).{hint}"
        )

    resolved_service_id = (
        service_id or resolve_railway_web_service_id(project, token, resolved_project_id) or ""
    ).strip()
    if not resolved_service_id:
        raise RuntimeError(
            "Could not resolve Railway web service ID — add RAILWAY_SERVICE_ID to vault or ensure "
            "the web service name matches the project (not Postgres)."
        )

    from apps.projects.scan_data_utils import update_project_scan_data
    from .railway_postgres import postgres_reference_for_preset

    merged_variables = dict(variables or {})
    postgres_ref = postgres_reference_for_preset(resolved_preset, slug=project.slug)
    if postgres_ref:
        merged_variables.setdefault("DATABASE_URL", postgres_ref)

    result = push_vault_env_to_platform(
        project,
        "railway",
        resolved_service_id,
        project_id=resolved_project_id,
        preset=resolved_preset,
        variables=merged_variables or None,
    )
    remember_railway_targets(project, resolved_project_id, resolved_service_id)

    sync_production_url_from_railway(
        project, token, resolved_project_id, resolved_service_id
    )

    update_project_scan_data(
        project,
        {
            "railway": {
                "lastEnvPushAt": datetime.now(timezone.utc).isoformat(),
                "lastPushedKeys": result.get("pushed") or [],
            }
        },
    )

    result["projectId"] = resolved_project_id
    result["serviceId"] = resolved_service_id

    try:
        from .railway_deploy import ensure_railway_github_and_deploy

        deploy_result = ensure_railway_github_and_deploy(
            project,
            token,
            resolved_project_id,
            resolved_service_id,
        )
        result["railwayDeploy"] = deploy_result
        if deploy_result.get("deployTriggered"):
            pushed = list(result.get("pushed") or [])
            result["message"] = (
                f"Pushed {len(pushed)} var(s); {deploy_result.get('message', 'deploy triggered')}"
            )
        elif deploy_result.get("repoConnected"):
            result["message"] = deploy_result.get("message", result.get("message", ""))
    except RuntimeError as exc:
        result["railwayDeploy"] = {"ok": False, "message": str(exc)}

    return result
