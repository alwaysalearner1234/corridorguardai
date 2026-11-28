"""Task 8: the PayPal sandbox client.

Talks to the PayPal sandbox over httpx with a 10 second timeout. Three things
matter here:

* **Sandbox only.** `require_sandbox_url` refuses any host that is not the
  PayPal sandbox, so the app cannot be pointed at production by accident.
* **Secrets stay secret.** The client id, the client secret and the access token
  come from environment variables, are never written to the log and never sent
  anywhere except PayPal.
* **Nothing is built from user input.** The URLs are fixed paths under the
  sandbox host.

What was checked against PayPal's own documentation is written down in
`docs/paypal-notes.md`.
"""

from __future__ import annotations

import base64
import logging
import os
import time
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any
from urllib.parse import urlsplit

import httpx

logger = logging.getLogger("app.paypal")

#: The sandbox host PayPal documents for the REST APIs.
SANDBOX_BASE_URL = "https://api-m.sandbox.paypal.com"

#: Every host that counts as the sandbox.
SANDBOX_HOSTS = frozenset({"api-m.sandbox.paypal.com", "api.sandbox.paypal.com"})

#: PayPal asks for a timeout on every call.
TIMEOUT_S = 10.0

#: The headers PayPal puts on every webhook delivery.
HEADER_TRANSMISSION_ID = "paypal-transmission-id"
HEADER_TRANSMISSION_TIME = "paypal-transmission-time"
HEADER_TRANSMISSION_SIG = "paypal-transmission-sig"
HEADER_CERT_URL = "paypal-cert-url"
HEADER_AUTH_ALGO = "paypal-auth-algo"

#: All of these have to be there before a delivery can be checked.
REQUIRED_TRANSMISSION_HEADERS = (
    HEADER_TRANSMISSION_ID,
    HEADER_TRANSMISSION_TIME,
    HEADER_TRANSMISSION_SIG,
    HEADER_CERT_URL,
    HEADER_AUTH_ALGO,
)


class PayPalError(RuntimeError):
    """Something went wrong while talking to PayPal."""


class PayPalConfigError(PayPalError):
    """The client is not configured safely, for example a production URL."""


def require_sandbox_url(url: str) -> str:
    """Return the URL unchanged, but only if it points at the PayPal sandbox.

    Anything else - production, a typo, a look-alike host - is refused.
    """
    candidate = (url or "").strip()
    host = (urlsplit(candidate).hostname or "").lower()
    if host not in SANDBOX_HOSTS:
        allowed = ", ".join(sorted(SANDBOX_HOSTS))
        raise PayPalConfigError(
            f"PAYPAL_BASE_URL must point at the PayPal sandbox ({allowed}). "
            f"This demo never talks to production PayPal."
        )
    return candidate.rstrip("/")


def _env(name: str, default: str = "") -> str:
    value = os.getenv(name)
    return value.strip() if value else default


def normalise_headers(headers: Mapping[str, str] | None) -> dict[str, str]:
    """Header names in lower case, so the lookup does not depend on the case."""
    return {str(key).lower(): str(value) for key, value in (headers or {}).items()}


class PayPalClient:
    """A small client for the three PayPal calls this project needs."""

    def __init__(
        self,
        base_url: str | None = None,
        client_id: str | None = None,
        client_secret: str | None = None,
        webhook_id: str | None = None,
        timeout_s: float | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.base_url = require_sandbox_url(
            base_url or _env("PAYPAL_BASE_URL", SANDBOX_BASE_URL) or SANDBOX_BASE_URL
        )
        self._client_id = client_id if client_id is not None else _env("PAYPAL_CLIENT_ID")
        self._client_secret = (
            client_secret if client_secret is not None else _env("PAYPAL_CLIENT_SECRET")
        )
        self._webhook_id = webhook_id if webhook_id is not None else _env("PAYPAL_WEBHOOK_ID")
        self.timeout_s = float(
            timeout_s if timeout_s is not None else _env("PAYPAL_TIMEOUT_S", str(TIMEOUT_S))
        )
        self._transport = transport
        self._token: str | None = None
        self._token_expires_at: float = 0.0

    # ---- credentials -------------------------------------------------------
    @property
    def webhook_id(self) -> str:
        """The webhook id from the Developer Portal. Never logged."""
        return self._webhook_id

    @property
    def has_credentials(self) -> bool:
        return bool(self._client_id and self._client_secret)

    def _basic_auth_header(self) -> str:
        if not self.has_credentials:
            raise PayPalConfigError(
                "PAYPAL_CLIENT_ID and PAYPAL_CLIENT_SECRET must be set to call PayPal."
            )
        raw = f"{self._client_id}:{self._client_secret}".encode()
        return "Basic " + base64.b64encode(raw).decode()

    # ---- transport ---------------------------------------------------------
    def _http(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            base_url=self.base_url,
            timeout=self.timeout_s,
            transport=self._transport,
        )

    async def get_access_token(self) -> str:
        """Exchange the client id and secret for an access token.

        The credentials only ever go to PayPal, in the Authorization header of
        the token request. Nothing about them is logged.
        """
        try:
            async with self._http() as http:
                response = await http.post(
                    "/v1/oauth2/token",
                    data={"grant_type": "client_credentials"},
                    headers={
                        "Authorization": self._basic_auth_header(),
                        "Content-Type": "application/x-www-form-urlencoded",
                    },
                )
        except httpx.HTTPError as exc:
            raise PayPalError("Could not reach PayPal for an access token.") from exc
        if response.status_code != 200:
            # The body can repeat the credentials, so it is never logged.
            raise PayPalError(
                f"PayPal answered {response.status_code} for the access token request."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PayPalError("PayPal sent an access token that is not JSON.") from exc
        token = payload.get("access_token")
        if not token:
            raise PayPalError("PayPal did not send an access token.")
        expires_in = payload.get("expires_in")
        lifetime = float(expires_in) if isinstance(expires_in, int | float) else 300.0
        self._token = str(token)
        self._token_expires_at = time.monotonic() + max(lifetime - 60.0, 30.0)
        return self._token

    async def _bearer(self) -> str:
        if self._token is None or time.monotonic() >= self._token_expires_at:
            await self.get_access_token()
        assert self._token is not None
        return self._token

    # ---- orders ------------------------------------------------------------
    async def create_order(
        self, amount: str | float | Decimal, currency: str, end_to_end_id: str
    ) -> dict[str, Any]:
        """Create a sandbox order that carries our end-to-end id.

        PayPal calls the field `custom_id` on the purchase unit: "The API
        caller-provided external ID. Used to reconcile API caller-initiated
        transactions with PayPal transactions." That is the field Task 9 uses
        to match a PayPal event with a payment.
        """
        try:
            value = f"{Decimal(str(amount)):.2f}"
        except InvalidOperation as exc:
            raise PayPalError("The order amount is not a number.") from exc
        body = {
            "intent": "CAPTURE",
            "purchase_units": [
                {
                    "custom_id": end_to_end_id,
                    "amount": {
                        "currency_code": currency.upper(),
                        "value": value,
                    },
                }
            ],
        }
        token = await self._bearer()
        try:
            async with self._http() as http:
                response = await http.post(
                    "/v2/checkout/orders",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                        # PayPal's own idempotency header: the same end-to-end id
                        # never creates a second order.
                        "PayPal-Request-Id": end_to_end_id,
                    },
                )
        except httpx.HTTPError as exc:
            raise PayPalError("Could not reach PayPal to create the order.") from exc
        if response.status_code not in (200, 201):
            raise PayPalError(f"PayPal answered {response.status_code} for the order.")
        try:
            return dict(response.json())
        except ValueError as exc:
            raise PayPalError("PayPal sent an order that is not JSON.") from exc

    # ---- webhooks ----------------------------------------------------------
    async def verify_webhook_signature(
        self, headers: Mapping[str, str] | None, event: dict[str, Any]
    ) -> bool:
        """Ask PayPal whether this delivery really came from PayPal.

        PayPal's own words: "Post the message, stored webhook ID, and header
        information back to PayPal's verify signature endpoint for it to do this
        check for you." The answer is `verification_status`: SUCCESS or FAILURE.
        """
        sent = normalise_headers(headers)
        if any(not sent.get(name) for name in REQUIRED_TRANSMISSION_HEADERS):
            return False
        if not self._webhook_id:
            logger.warning("PAYPAL_WEBHOOK_ID is not set, so no webhook can be verified.")
            return False

        body = {
            "auth_algo": sent[HEADER_AUTH_ALGO],
            "cert_url": sent[HEADER_CERT_URL],
            "transmission_id": sent[HEADER_TRANSMISSION_ID],
            "transmission_sig": sent[HEADER_TRANSMISSION_SIG],
            "transmission_time": sent[HEADER_TRANSMISSION_TIME],
            "webhook_id": self._webhook_id,
            "webhook_event": event,
        }
        token = await self._bearer()
        try:
            async with self._http() as http:
                response = await http.post(
                    "/v1/notifications/verify-webhook-signature",
                    json=body,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "Content-Type": "application/json",
                    },
                )
        except httpx.HTTPError as exc:
            raise PayPalError("Could not reach PayPal to verify the webhook.") from exc
        if response.status_code != 200:
            raise PayPalError(
                f"PayPal answered {response.status_code} for the signature check."
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise PayPalError("PayPal sent a signature answer that is not JSON.") from exc
        status = str(payload.get("verification_status") or "").upper()
        return status == "SUCCESS"
