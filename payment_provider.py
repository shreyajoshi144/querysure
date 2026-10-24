"""Provider-neutral payment boundary.

Three layers, deliberately separate:

* ``PaymentProvider`` -- the abstract contract a payment integration implements. It has no behaviour of its own.
* ``VerifiedPaymentEvent`` -- the ONE normalized shape a provider event takes after ``verify_event`` has authenticated it.
  Nothing outside an adapter ever sees a provider-specific payload, and nothing but an authenticated payload may become one.
* ``GenericHmacProvider`` -- a vendor-neutral reference adapter (timestamped HMAC-SHA256 webhook signatures + a signed
  hosted-checkout link). It is NOT an integration with any particular payment company. Wiring a real one means subclassing
  ``PaymentProvider``, implementing ``verify_event`` / ``create_checkout_session`` for that company's wire format, and
  calling ``register_provider``; see docs/BILLING.md.

Selection is by the ``PAYMENT_PROVIDER`` setting. When it is empty billing is disabled: ``get_payment_provider`` returns
``None`` and QuerySure performs no payment-provider calls at all.

Trust rule: the browser never says what plan or billing status an account has. A webhook is mapped to a user only through a
server-created checkout reference or a provider subscription/customer id already stored by QuerySure -- never through a
user id, e-mail or plan carried in a payload.
"""
from __future__ import annotations

import hashlib
import hmac
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable
from urllib.parse import urlencode

import config

# The normalized event kinds the billing service understands. Anything else a provider sends is acknowledged as "ignored".
EVENT_KINDS = frozenset({
    "checkout_completed",
    "subscription_updated",
    "payment_succeeded",
    "payment_failed",
    "payment_refunded",
    "ignored",
})
_PAYMENT_STATUSES = {"payment_succeeded": "succeeded", "payment_failed": "failed", "payment_refunded": "refunded"}


class PaymentProviderError(Exception):
    """Base class for every error raised by a payment adapter."""


class ProviderNotConfigured(PaymentProviderError):
    """Billing is disabled, or the selected adapter is missing required settings."""


class ProviderNotSupported(PaymentProviderError):
    """The adapter does not implement this operation (e.g. cancelling from QuerySure)."""


class ProviderRequestError(PaymentProviderError):
    """A call to the provider failed (network, 5xx, rejected request)."""


class WebhookVerificationError(PaymentProviderError):
    """The signature is missing, malformed, stale (replay window) or does not match. Always answered with a bare 400."""


class InvalidEventError(PaymentProviderError):
    """The payload is authentic but not a well-formed event. Answered 400 so the integration bug is loud on the provider side."""


@dataclass(frozen=True)
class VerifiedPaymentEvent:
    """Provider-neutral, ALREADY-AUTHENTICATED billing event.

    ``occurred_at`` is when the provider says the event happened (not when it arrived); it is what makes out-of-order
    delivery safe. ``checkout_reference`` is the id of a QuerySure ``CheckoutSession`` (our value, echoed back).
    """

    provider: str
    event_id: str
    event_type: str            # the provider's own type string, kept for audit only
    kind: str                  # one of EVENT_KINDS
    occurred_at: datetime      # timezone-aware UTC
    plan: str | None = None
    status: str | None = None
    checkout_reference: str | None = None
    provider_customer_id: str | None = None
    provider_subscription_id: str | None = None
    current_period_start: datetime | None = None
    current_period_end: datetime | None = None
    cancel_at_period_end: bool = False
    payment_id: str | None = None
    amount_minor: int | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if self.kind not in EVENT_KINDS:
            raise InvalidEventError(f"unknown event kind {self.kind!r}")
        if self.occurred_at.tzinfo is None:
            raise InvalidEventError("occurred_at must be timezone-aware")

    @property
    def payment_status(self) -> str | None:
        return _PAYMENT_STATUSES.get(self.kind)


@dataclass(frozen=True)
class CheckoutResult:
    """What ``create_checkout_session`` returns: where to send the browser, plus the provider's own session id."""

    url: str
    provider_session_id: str | None = None


class PaymentProvider(ABC):
    """Abstract boundary for a payment-provider adapter.

    Implementations belong in their own module. QuerySure must never treat client input as a verified billing event, so
    ``verify_event`` is the only way to produce a ``VerifiedPaymentEvent``.
    """

    # Header that carries the webhook signature for this provider.
    signature_header: str = "X-Signature"

    @property
    @abstractmethod
    def provider_name(self) -> str:
        raise NotImplementedError

    @abstractmethod
    def create_customer(self, *, user_id: str, email: str, **kwargs: Any) -> Any:
        """Create a provider-side customer; implemented only by an adapter."""
        raise NotImplementedError

    @abstractmethod
    def create_checkout_session(self, *, customer_id: str, plan: str, **kwargs: Any) -> Any:
        """Create a provider checkout session (return a ``CheckoutResult``); implemented only by an adapter.

        QuerySure passes ``reference`` (its CheckoutSession id, to be echoed back in webhooks), ``return_url`` and
        ``expires_at`` as keyword arguments."""
        raise NotImplementedError

    @abstractmethod
    def cancel_subscription(self, *, subscription_id: str, **kwargs: Any) -> Any:
        """Cancel a provider-side subscription; implemented only by an adapter."""
        raise NotImplementedError

    @abstractmethod
    def get_subscription(self, *, subscription_id: str, **kwargs: Any) -> Any:
        """Retrieve provider subscription state; implemented only by an adapter."""
        raise NotImplementedError

    @abstractmethod
    def verify_event(self, payload: bytes, signature: str | None) -> VerifiedPaymentEvent:
        """Authenticate an external event and return normalized billing state.

        Must raise ``WebhookVerificationError`` unless the signature proves the payload came from the provider, and
        ``InvalidEventError`` for an authentic but malformed payload."""
        raise NotImplementedError


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------
_FACTORIES: dict[str, Callable[[], PaymentProvider]] = {}


def register_provider(name: str, factory: Callable[[], PaymentProvider]) -> None:
    """Make an adapter selectable via ``PAYMENT_PROVIDER=<name>``."""
    _FACTORIES[name.strip().lower()] = factory


def billing_enabled() -> bool:
    """True when ``PAYMENT_PROVIDER`` names a registered adapter. Never instantiates an adapter or touches the network."""
    return bool(config.PAYMENT_PROVIDER) and config.PAYMENT_PROVIDER in _FACTORIES


def get_payment_provider(name: str | None = None) -> PaymentProvider | None:
    """The configured adapter, or ``None`` when billing is disabled. Raises ``ProviderNotConfigured`` for an unknown name."""
    selected = (config.PAYMENT_PROVIDER if name is None else name or "").strip().lower()
    if not selected:
        return None
    factory = _FACTORIES.get(selected)
    if factory is None:
        raise ProviderNotConfigured(f"PAYMENT_PROVIDER {selected!r} is not a registered payment adapter")
    return factory()


def plan_map() -> dict[str, str]:
    """Provider price/plan id -> QuerySure plan key, from BILLING_PLAN_MAP_JSON (empty when unset or invalid)."""
    raw = config.BILLING_PLAN_MAP_JSON
    if not raw:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return {str(k): str(v).lower() for k, v in data.items()} if isinstance(data, dict) else {}


# ---------------------------------------------------------------------------
# Vendor-neutral reference adapter
# ---------------------------------------------------------------------------
def _epoch_to_dt(value: Any, field: str) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0 or value > 4_102_444_800:
        raise InvalidEventError(f"{field} must be a unix timestamp")
    return datetime.fromtimestamp(float(value), tz=timezone.utc)


def _opt_epoch(value: Any, field: str) -> datetime | None:
    return None if value is None else _epoch_to_dt(value, field)


def _opt_str(value: Any, field: str, limit: int = 255) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or len(value) > limit:
        raise InvalidEventError(f"{field} must be a non-empty string")
    return value


class GenericHmacProvider(PaymentProvider):
    """Reference adapter for any gateway that can deliver JSON events signed as below.

    Webhook wire format -- header ``X-Signature: t=<unix seconds>,v1=<hex>[,v1=<hex>...]`` where
    ``v1 = HMAC_SHA256(secret, "<t>." + raw_body)``. A delivery is accepted only if ``t`` is within the replay window and
    some ``v1`` matches some configured secret (several secrets may be configured, to rotate without downtime).

    Body::

        {"id": "evt_1", "type": "subscription.updated", "created": 1700000000,
         "data": {"reference": "<checkout id>", "customer_id": "...", "subscription_id": "...", "plan": "pro",
                  "status": "active", "current_period_start": 1700000000, "current_period_end": 1702592000,
                  "cancel_at_period_end": false}}

    ``payment.succeeded|failed|refunded`` carry ``payment_id``, ``subscription_id``, ``amount_minor``, ``currency``.
    Checkout links are signed with a separate secret so the plan and reference cannot be altered in transit.
    Cancelling and live subscription lookup are done in the provider's own portal (``PAYMENT_PORTAL_URL``).
    """

    signature_header = "X-Signature"

    def __init__(
        self,
        *,
        webhook_secrets: tuple[str, ...] | None = None,
        checkout_url: str | None = None,
        checkout_signing_secret: str | None = None,
        portal_url: str | None = None,
        tolerance_seconds: int | None = None,
        plans: dict[str, str] | None = None,
        clock: Callable[[], float] = time.time,
        name: str = "generic",
    ) -> None:
        self._name = name
        self._secrets = tuple(webhook_secrets if webhook_secrets is not None else config.PAYMENT_WEBHOOK_SECRETS)
        self._checkout_url = checkout_url if checkout_url is not None else config.PAYMENT_CHECKOUT_URL
        self._checkout_secret = (
            checkout_signing_secret if checkout_signing_secret is not None else config.PAYMENT_CHECKOUT_SIGNING_SECRET
        )
        self.portal_url = (portal_url if portal_url is not None else config.PAYMENT_PORTAL_URL) or None
        self._tolerance = tolerance_seconds if tolerance_seconds is not None else config.BILLING_WEBHOOK_TOLERANCE_SECONDS
        self._plans = plans if plans is not None else plan_map()
        self._clock = clock
        if not self._secrets or any(len(s) < 16 for s in self._secrets):
            raise ProviderNotConfigured("PAYMENT_WEBHOOK_SECRETS must contain at least one secret of 16+ characters")

    @property
    def provider_name(self) -> str:
        return self._name

    # -- checkout ---------------------------------------------------------------------------------------------------
    def create_customer(self, *, user_id: str, email: str, **kwargs: Any) -> dict:
        # No remote call and the e-mail is deliberately not used: the id is a stable pseudonym of the user.
        key = (self._checkout_secret or self._secrets[0]).encode("utf-8")
        return {"customer_id": "cus_" + hmac.new(key, user_id.encode("utf-8"), hashlib.sha256).hexdigest()[:24]}

    def create_checkout_session(self, *, customer_id: str, plan: str, **kwargs: Any) -> CheckoutResult:
        if not self._checkout_url or not self._checkout_secret:
            raise ProviderNotConfigured("PAYMENT_CHECKOUT_URL and PAYMENT_CHECKOUT_SIGNING_SECRET are required")
        reference = kwargs.get("reference")
        expires_at = kwargs.get("expires_at")
        if not reference or not isinstance(expires_at, datetime):
            raise ProviderRequestError("checkout reference and expiry are required")
        params = {"reference": reference, "plan": plan, "customer": customer_id or "", "exp": str(int(expires_at.timestamp()))}
        if kwargs.get("return_url"):
            params["return_url"] = kwargs["return_url"]
        canonical = urlencode(sorted(params.items()))
        params["sig"] = hmac.new(self._checkout_secret.encode("utf-8"), canonical.encode("utf-8"), hashlib.sha256).hexdigest()
        sep = "&" if "?" in self._checkout_url else "?"
        return CheckoutResult(url=self._checkout_url + sep + urlencode(params), provider_session_id=None)

    def cancel_subscription(self, *, subscription_id: str, **kwargs: Any) -> Any:
        raise ProviderNotSupported("This adapter cannot cancel subscriptions; use the provider's customer portal.")

    def get_subscription(self, *, subscription_id: str, **kwargs: Any) -> Any:
        raise ProviderNotSupported("This adapter cannot look up subscriptions; rely on webhooks.")

    # -- webhooks ---------------------------------------------------------------------------------------------------
    def _check_signature(self, payload: bytes, signature: str | None) -> None:
        if not signature or len(signature) > 1024:
            raise WebhookVerificationError("missing signature")
        timestamp: str | None = None
        candidates: list[str] = []
        for part in signature.split(","):
            key, sep, value = part.strip().partition("=")
            if not sep:
                raise WebhookVerificationError("malformed signature")
            if key == "t":
                timestamp = value
            elif key == "v1":
                candidates.append(value)
        if not timestamp or not timestamp.isdigit() or not candidates:
            raise WebhookVerificationError("malformed signature")
        if abs(self._clock() - int(timestamp)) > self._tolerance:
            raise WebhookVerificationError("timestamp outside the replay window")
        signed = timestamp.encode("ascii") + b"." + payload
        matched = False
        for secret in self._secrets:
            expected = hmac.new(secret.encode("utf-8"), signed, hashlib.sha256).hexdigest()
            for candidate in candidates:
                # Evaluate every pair (no early exit) so timing does not reveal which secret or candidate was close.
                matched |= hmac.compare_digest(expected, candidate.lower())
        if not matched:
            raise WebhookVerificationError("signature mismatch")

    def verify_event(self, payload: bytes, signature: str | None) -> VerifiedPaymentEvent:
        self._check_signature(payload, signature)
        try:
            body = json.loads(payload.decode("utf-8"))
        except (UnicodeDecodeError, ValueError) as exc:
            raise InvalidEventError("body is not valid JSON") from exc
        if not isinstance(body, dict):
            raise InvalidEventError("body must be an object")
        event_id = _opt_str(body.get("id"), "id")
        event_type = _opt_str(body.get("type"), "type", 96)
        if not event_id or not event_type:
            raise InvalidEventError("id and type are required")
        occurred_at = _epoch_to_dt(body.get("created"), "created")
        data = body.get("data")
        if not isinstance(data, dict):
            raise InvalidEventError("data must be an object")

        common = dict(provider=self._name, event_id=event_id, event_type=event_type, occurred_at=occurred_at)
        family, _, action = event_type.partition(".")

        if family == "payment" and action in ("succeeded", "failed", "refunded"):
            amount = data.get("amount_minor")
            if amount is not None and (isinstance(amount, bool) or not isinstance(amount, int) or amount < 0):
                raise InvalidEventError("amount_minor must be a non-negative integer")
            currency = _opt_str(data.get("currency"), "currency", 3)
            if currency is not None and len(currency) != 3:
                raise InvalidEventError("currency must be a 3-letter code")
            payment_id = _opt_str(data.get("payment_id"), "payment_id")
            if not payment_id:
                raise InvalidEventError("payment_id is required")
            return VerifiedPaymentEvent(
                kind="payment_" + action, payment_id=payment_id, amount_minor=amount,
                currency=currency.upper() if currency else None,
                provider_subscription_id=_opt_str(data.get("subscription_id"), "subscription_id"),
                provider_customer_id=_opt_str(data.get("customer_id"), "customer_id"),
                checkout_reference=_opt_str(data.get("reference"), "reference", 36), **common,
            )

        if (family == "checkout" and action == "completed") or (family == "subscription" and action in ("created", "updated", "canceled")):
            status = _opt_str(data.get("status"), "status", 32)
            if action == "canceled":
                status = status or "canceled"
            plan_raw = _opt_str(data.get("plan"), "plan", 96)
            plan = None
            if plan_raw is not None:
                plan = plan_raw.lower() if plan_raw.lower() in _known_plans() else self._plans.get(plan_raw)
                if plan is None:
                    raise InvalidEventError("unknown plan")
            kind = "checkout_completed" if family == "checkout" else "subscription_updated"
            if kind == "subscription_updated" and (status is None or plan is None):
                raise InvalidEventError("subscription events require plan and status")
            return VerifiedPaymentEvent(
                kind=kind, plan=plan, status=status.lower() if status else None,
                checkout_reference=_opt_str(data.get("reference"), "reference", 36),
                provider_customer_id=_opt_str(data.get("customer_id"), "customer_id"),
                provider_subscription_id=_opt_str(data.get("subscription_id"), "subscription_id"),
                current_period_start=_opt_epoch(data.get("current_period_start"), "current_period_start"),
                current_period_end=_opt_epoch(data.get("current_period_end"), "current_period_end"),
                cancel_at_period_end=bool(data.get("cancel_at_period_end", False)), **common,
            )

        return VerifiedPaymentEvent(kind="ignored", **common)


def _known_plans() -> frozenset[str]:
    from entitlements import PLAN_CONFIG
    return frozenset(PLAN_CONFIG)


def _generic_from_config() -> PaymentProvider:
    return GenericHmacProvider()


register_provider("generic", _generic_from_config)
