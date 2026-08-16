"""Stripe catalog provisioning — port of api-automation.ts."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse

import stripe

from apps.projects.models import Project
from apps.vault.models import set_secret

from .events import EventEmitter, PipelineEvent, emit

INSTALLER_TAG = "stripe-installer"

# Pipeline event name constants
_EVT_PRODUCTS = "provision.products"
_EVT_PORTAL = "provision.portal"
_EVT_WEBHOOK = "provision.webhook"

DEFAULT_TIERS = [
    {
        "name": "Starter",
        "description": "Essential features",
        "amount": 900,
        "currency": "usd",
        "interval": "month",
        "trialDays": 14,
        "features": ["Core features", "Email support"],
    },
    {
        "name": "Pro",
        "description": "Growing teams",
        "amount": 2900,
        "currency": "usd",
        "interval": "month",
        "trialDays": 14,
        "features": ["Everything in Starter", "Priority support", "Team seats"],
    },
    {
        "name": "Enterprise",
        "description": "Full access",
        "amount": 99000,
        "currency": "usd",
        "interval": "year",
        "trialDays": 0,
        "features": ["Custom SLA", "Dedicated support", "SSO"],
    },
]

DEFAULT_WEBHOOK_EVENTS = [
    "checkout.session.completed",
    "customer.subscription.created",
    "customer.subscription.updated",
    "customer.subscription.deleted",
    "invoice.paid",
    "invoice.payment_failed",
    "customer.created",
    "account.updated",
    "transfer.created",
    "transfer.updated",
    "transfer.reversed",
]


@dataclass
class ProvisionConfig:
    tiers: list[dict[str, Any]] | None = None
    product_name: str | None = None
    product_description: str | None = None
    one_time_amount: int | None = None
    currency: str = "usd"
    webhook_url: str | None = None
    webhook_events: list[str] | None = None
    billing_portal_return_url: str | None = None
    app_url: str = "http://localhost:8000"
    reuse_existing: bool = True
    create_webhook: bool = True
    create_portal: bool = True


@dataclass
class ProvisionResult:
    products: list[dict[str, Any]] = field(default_factory=list)
    prices: list[dict[str, Any]] = field(default_factory=list)
    webhook_endpoint: dict[str, Any] | None = None
    billing_portal_config: dict[str, Any] | None = None
    webhook_secret_stored: bool = False
    warnings: list[str] = field(default_factory=list)


def _manifest_path(project_root: Path) -> Path:
    return project_root / ".stripe-installer" / "stripe-manifest.json"


def load_manifest(project_root: Path) -> dict | None:
    path = _manifest_path(project_root)
    if not path.is_file():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def save_manifest(project_root: Path, manifest: dict) -> None:
    path = _manifest_path(project_root)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(manifest, indent=2), encoding="utf-8")


def format_amount(cents: int, currency: str) -> str:
    if currency.lower() == "usd":
        return f"${cents / 100:.2f}"
    return f"{cents / 100:.2f} {currency.upper()}"


def _find_product_by_name(name: str) -> stripe.Product | None:
    products = stripe.Product.list(limit=100, active=True)
    for product in products.data:
        if product.name == name and _stripe_metadata(product).get("created_by") == INSTALLER_TAG:
            return product
    return None


def _find_or_create_product(
    name: str,
    description: str | None,
    reuse: bool,
) -> tuple[str, str, bool]:
    if reuse:
        existing = _find_product_by_name(name)
        if existing:
            return existing.id, existing.name, True

    product = stripe.Product.create(
        name=name,
        description=description,
        metadata={"created_by": INSTALLER_TAG, "tier": name},
    )
    return product.id, product.name, False


def _create_price(
    product_id: str,
    amount: int,
    currency: str,
    *,
    interval: str | None = None,
    tier_name: str = "default",
    trial_days: int = 0,
) -> stripe.Price:
    params: dict[str, Any] = {
        "product": product_id,
        "unit_amount": amount,
        "currency": currency,
        "metadata": {"created_by": INSTALLER_TAG, "tier": tier_name},
    }
    if interval:
        recurring: dict[str, Any] = {"interval": interval}
        if trial_days > 0:
            recurring["trial_period_days"] = trial_days
        params["recurring"] = recurring
    return stripe.Price.create(**params)


def _find_or_create_price(
    product_id: str,
    amount: int,
    currency: str,
    reuse: bool,
    *,
    interval: str | None = None,
    tier_name: str = "default",
    trial_days: int = 0,
) -> tuple[str, bool]:
    if reuse:
        prices = stripe.Price.list(product=product_id, active=True, limit=100)
        for price in prices.data:
            metadata = _stripe_metadata(price)
            if (
                price.unit_amount == amount
                and price.currency == currency
                and metadata.get("created_by") == INSTALLER_TAG
                and metadata.get("tier") == tier_name
                and (
                    (interval and price.recurring and price.recurring.interval == interval)
                    or (not interval and not price.recurring)
                )
            ):
                return price.id, True

    created = _create_price(
        product_id,
        amount,
        currency,
        interval=interval,
        tier_name=tier_name,
        trial_days=trial_days,
    )
    return created.id, False


def _resolve_tier_product(
    tier: dict[str, Any],
    manifest: dict | None,
    reuse: bool,
) -> tuple[str, bool]:
    name = tier["name"]
    manifest_product = next(
        (p for p in (manifest or {}).get("products", []) if p.get("name") == name),
        None,
    )
    if reuse and manifest_product:
        return manifest_product["id"], True
    product_id, _, reused = _find_or_create_product(name, tier.get("description"), reuse)
    return product_id, reused


def _resolve_tier_price(
    tier: dict[str, Any],
    product_id: str,
    manifest: dict | None,
    reuse: bool,
) -> tuple[str, bool]:
    name = tier["name"]
    manifest_price = next(
        (
            p
            for p in (manifest or {}).get("prices", [])
            if p.get("tier") == name
            and p.get("amount") == tier["amount"]
            and p.get("interval") == tier.get("interval")
        ),
        None,
    )
    if reuse and manifest_price:
        return manifest_price["id"], True
    return _find_or_create_price(
        product_id,
        tier["amount"],
        tier.get("currency", "usd"),
        reuse,
        interval=tier.get("interval"),
        tier_name=name,
        trial_days=tier.get("trialDays", 0),
    )


def _create_subscription_catalog(
    tiers: list[dict[str, Any]],
    manifest: dict | None,
    reuse: bool,
    on_event: EventEmitter | None,
) -> tuple[list[dict], list[dict]]:
    products: list[dict] = []
    prices: list[dict] = []

    for tier in tiers:
        name = tier["name"]
        product_id, product_reused = _resolve_tier_product(tier, manifest, reuse)
        products.append({"id": product_id, "name": name, "reused": product_reused})

        price_id, price_reused = _resolve_tier_price(tier, product_id, manifest, reuse)
        prices.append(
            {
                "id": price_id,
                "tier": name,
                "amount": tier["amount"],
                "currency": tier.get("currency", "usd"),
                "interval": tier.get("interval"),
                "trialDays": tier.get("trialDays"),
                "reused": price_reused,
            }
        )

        if not price_reused:
            label = format_amount(tier["amount"], tier.get("currency", "usd"))
            interval_str = f"/{tier['interval']}" if tier.get("interval") else ""
            emit(
                on_event,
                PipelineEvent(
                    "provision.price",
                    "detail",
                    f"Created: {name} ({label}{interval_str})",
                    detail=True,
                ),
            )

    return products, prices


def _build_portal_products(price_ids: list[str]) -> list[dict[str, Any]]:
    # Stripe billing portal requires at most one price per (product, interval).
    # Track seen intervals per product and skip duplicates.
    product_map: dict[str, list[str]] = {}
    seen_intervals: dict[str, set[str | None]] = {}
    for price_id in price_ids:
        price = stripe.Price.retrieve(price_id)
        product_id = price.product if isinstance(price.product, str) else price.product.id
        interval = price.recurring.interval if price.recurring else None
        if interval in seen_intervals.get(product_id, set()):
            continue
        product_map.setdefault(product_id, []).append(price_id)
        seen_intervals.setdefault(product_id, set()).add(interval)
    return [{"product": pid, "prices": pids} for pid, pids in product_map.items()]


def _stripe_metadata(obj: Any) -> dict[str, Any]:
    metadata = getattr(obj, "metadata", None) or {}
    if isinstance(metadata, dict):
        return metadata
    if hasattr(metadata, "to_dict_recursive"):
        return metadata.to_dict_recursive()
    if hasattr(metadata, "to_dict"):
        return metadata.to_dict()
    if hasattr(metadata, "keys"):
        return {key: metadata[key] for key in metadata.keys()}
    return {}


def _configure_billing_portal(
    return_url: str,
    manifest: dict | None,
    reuse: bool,
    on_event: EventEmitter | None,
) -> dict[str, Any]:
    emit(on_event, PipelineEvent(_EVT_PORTAL, "running", "Configuring billing portal…"))

    if reuse and manifest and (manifest.get("billingPortalConfig") or {}).get("id"):
        try:
            existing = stripe.billing_portal.Configuration.retrieve(
                manifest["billingPortalConfig"]["id"]
            )
            if existing.active:
                emit(on_event, PipelineEvent(_EVT_PORTAL, "ok", "Billing portal reused"))
                return {"id": existing.id, "reused": True}
        except stripe.error.StripeError:
            pass

    prices = stripe.Price.list(active=True, limit=50, type="recurring")
    installer_prices = [p for p in prices.data if _stripe_metadata(p).get("created_by") == INSTALLER_TAG]
    source = installer_prices if installer_prices else prices.data
    price_ids = [p.id for p in source[:10]]

    features: dict[str, Any] = {
        "customer_update": {"enabled": True, "allowed_updates": ["email", "address", "name"]},
        "invoice_history": {"enabled": True},
        "payment_method_update": {"enabled": True},
        "subscription_cancel": {"enabled": True, "mode": "at_period_end"},
    }
    if price_ids:
        features["subscription_update"] = {
            "enabled": True,
            "default_allowed_updates": ["price", "promotion_code"],
            "products": _build_portal_products(price_ids),
        }
    else:
        features["subscription_update"] = {"enabled": False}

    portal = stripe.billing_portal.Configuration.create(
        business_profile={"headline": "Manage your subscription"},
        features=features,
        default_return_url=return_url,
        metadata={"created_by": INSTALLER_TAG},
    )
    emit(on_event, PipelineEvent(_EVT_PORTAL, "ok", f"Billing portal configured ({portal.id})"))
    return {"id": portal.id, "reused": False}


_LOCAL_PREFIXES = ("localhost", "127.", "0.0.0.0", "::1", "10.", "192.168.", "172.")


def _is_public_url(url: str) -> bool:
    host = urlparse(url).hostname or ""
    return not any(host == p.rstrip(".") or host.startswith(p) for p in _LOCAL_PREFIXES)


def retire_legacy_stripe_webhooks() -> list[dict[str, Any]]:
    """
    Delete retired/merged webhook endpoints from Stripe (e.g. api-transfer-production).
    Called before register/bootstrap so legacy URLs are not left alongside new ones.
    """
    from apps.stripe_core.portfolio_catalog import retired_webhook_hosts, retired_webhook_urls

    retired_urls = {u.rstrip("/") for u in retired_webhook_urls()}
    retired_hosts = retired_webhook_hosts()
    removed: list[dict[str, Any]] = []

    for ep in stripe.WebhookEndpoint.list(limit=100).data:
        url = (ep.url or "").rstrip("/")
        if not url:
            continue
        host = (urlparse(url).hostname or "").lower()
        if url not in retired_urls and host not in retired_hosts:
            continue
        try:
            stripe.WebhookEndpoint.delete(ep.id)
            removed.append({"id": ep.id, "url": url, "action": "deleted"})
        except stripe.StripeError as exc:
            removed.append({"id": ep.id, "url": url, "action": "error", "message": str(exc)})
    return removed


def _retire_superseded_host_webhooks(keep_url: str) -> list[str]:
    """Remove other webhook endpoints on the same host (wrong legacy path)."""
    keep = keep_url.rstrip("/")
    host = (urlparse(keep).hostname or "").lower()
    if not host:
        return []
    removed: list[str] = []
    for ep in stripe.WebhookEndpoint.list(limit=100).data:
        url = (ep.url or "").rstrip("/")
        if not url or url == keep:
            continue
        if (urlparse(url).hostname or "").lower() != host:
            continue
        stripe.WebhookEndpoint.delete(ep.id)
        removed.append(url)
    return removed


def _register_webhook(url: str, events: list[str]) -> dict[str, Any]:
    endpoints = stripe.WebhookEndpoint.list(limit=100)

    normalized = url.rstrip("/")
    # Exact URL match — reuse as-is (path must match registry; legacy /stripe/webhook ≠ /api/v1/billing/webhook/)
    match = next((e for e in endpoints.data if (e.url or "").rstrip("/") == normalized), None)
    if match:
        current_url = match.url or ""
        updated = stripe.WebhookEndpoint.modify(
            match.id,
            url=url,
            enabled_events=events,
            disabled=False,
        )
        _retire_superseded_host_webhooks(normalized)
        return {
            "id": match.id,
            "url": getattr(updated, "url", url),
            "secret": None,
            "reused": True,
            "urlCorrected": current_url != url,
        }

    created = stripe.WebhookEndpoint.create(
        url=url,
        enabled_events=events,
        metadata={"created_by": INSTALLER_TAG},
    )
    _retire_superseded_host_webhooks(normalized)
    return {"id": created.id, "url": created.url, "secret": created.secret, "reused": False}


def _provision_products(
    cfg: ProvisionConfig,
    manifest: dict | None,
    on_event: EventEmitter | None,
) -> tuple[list[dict], list[dict]]:
    tiers = cfg.tiers if cfg.tiers is not None else DEFAULT_TIERS

    if tiers:
        emit(on_event, PipelineEvent(_EVT_PRODUCTS, "running", "Provisioning Stripe products…"))
        products, prices = _create_subscription_catalog(tiers, manifest, cfg.reuse_existing, on_event)
        emit(on_event, PipelineEvent(_EVT_PRODUCTS, "ok", "Products provisioned"))
        return products, prices

    if cfg.product_name:
        emit(on_event, PipelineEvent(_EVT_PRODUCTS, "running", "Provisioning product…"))
        product_id, name, reused = _find_or_create_product(
            cfg.product_name, cfg.product_description, cfg.reuse_existing
        )
        products = [{"id": product_id, "name": name, "reused": reused}]
        prices: list[dict] = []
        if cfg.one_time_amount:
            price_id, price_reused = _find_or_create_price(
                product_id, cfg.one_time_amount, cfg.currency, cfg.reuse_existing, tier_name="one-time"
            )
            prices.append(
                {
                    "id": price_id,
                    "tier": "one-time",
                    "amount": cfg.one_time_amount,
                    "currency": cfg.currency,
                    "reused": price_reused,
                }
            )
        emit(on_event, PipelineEvent(_EVT_PRODUCTS, "ok", "Product provisioned"))
        return products, prices

    return [], []


def _provision_portal(
    cfg: ProvisionConfig,
    manifest: dict | None,
    warnings: list[str],
    on_event: EventEmitter | None,
) -> dict[str, Any] | None:
    if not cfg.create_portal:
        return None
    portal_return = cfg.billing_portal_return_url or f"{cfg.app_url.rstrip('/')}/stripe/account/"
    try:
        return _configure_billing_portal(portal_return, manifest, cfg.reuse_existing, on_event)
    except Exception as exc:
        message = str(exc) or exc.__class__.__name__
        warnings.append(f"Billing portal skipped: {message}")
        emit(on_event, PipelineEvent(_EVT_PORTAL, "warning", f"Billing portal skipped: {message}"))
        return None


def _provision_webhook(
    cfg: ProvisionConfig,
    result: ProvisionResult,
    warnings: list[str],
    project: Project | None,
    store_secret: Callable[[str, str], None] | None,
    on_event: EventEmitter | None,
) -> None:
    webhook_url = cfg.webhook_url
    if not (cfg.create_webhook and webhook_url and _is_public_url(webhook_url)):
        return
    events = cfg.webhook_events or DEFAULT_WEBHOOK_EVENTS
    emit(on_event, PipelineEvent(_EVT_WEBHOOK, "running", "Registering webhooks…"))
    webhook = _register_webhook(webhook_url, events)
    result.webhook_endpoint = {"id": webhook["id"], "url": webhook["url"], "reused": webhook["reused"]}
    emit(on_event, PipelineEvent(_EVT_WEBHOOK, "ok", f"Webhook registered: {webhook_url}"))
    if webhook.get("secret"):
        if store_secret:
            store_secret("STRIPE_WEBHOOK_SECRET", webhook["secret"])
        elif project:
            set_secret(project, "STRIPE_WEBHOOK_SECRET", webhook["secret"])
        result.webhook_secret_stored = True
    elif webhook.get("reused"):
        warnings.append(
            "Webhook endpoint already exists — could not retrieve signing secret. "
            "Copy whsec_ from Stripe Dashboard → Webhooks → Signing secret."
        )


def provision_catalog(
    secret_key: str,
    project_root: Path,
    *,
    project: Project | None = None,
    config: ProvisionConfig | None = None,
    account_id: str | None = None,
    on_event: EventEmitter | None = None,
    store_secret: Callable[[str, str], None] | None = None,
) -> ProvisionResult:
    """Provision Stripe products, prices, portal, and webhooks."""
    stripe.api_key = secret_key
    cfg = config or ProvisionConfig()
    manifest = load_manifest(project_root)
    warnings: list[str] = []
    result = ProvisionResult(warnings=warnings)

    result.products, result.prices = _provision_products(cfg, manifest, on_event)
    result.billing_portal_config = _provision_portal(cfg, manifest, warnings, on_event)
    _provision_webhook(cfg, result, warnings, project, store_secret, on_event)

    if project:
        from apps.vault.models import get_secret
        publishable = get_secret(project, "STRIPE_PUBLISHABLE_KEY")
        if publishable:
            set_secret(project, "NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY", publishable)

    tiers = cfg.tiers if cfg.tiers is not None else DEFAULT_TIERS
    now = datetime.now(timezone.utc).isoformat()
    save_manifest(
        project_root,
        {
            "createdAt": manifest.get("createdAt") if manifest else now,
            "updatedAt": now,
            "accountId": account_id,
            "products": [{"id": p["id"], "name": p["name"]} for p in result.products],
            "prices": [
                {
                    "id": p["id"],
                    "tier": p["tier"],
                    "amount": p["amount"],
                    "currency": p["currency"],
                    "interval": p.get("interval"),
                    "trialDays": p.get("trialDays"),
                    "features": next(
                        (t.get("features") for t in (tiers or []) if t.get("name") == p["tier"]),
                        None,
                    ),
                }
                for p in result.prices
            ],
            "webhookEndpoint": result.webhook_endpoint,
            "billingPortalConfig": result.billing_portal_config,
            "appUrl": cfg.app_url,
        },
    )

    return result
