"""Task 8: the PayPal webhook route.

`POST /ingest/paypal/webhook` is where PayPal delivers its sandbox events.

The order of the steps matters and never changes:

1. **PayPal's signature check runs first.** A delivery that PayPal cannot
   confirm is answered with 400 and nothing at all is written to the graph.
2. **An event id that was seen before is dropped.** PayPal sends the same
   delivery again when it does not get a 2xx answer, and a replay must not
   create a second event.
3. **The event is turned into a `PayPalEventIn`** and handed to the Task 6
   ingest service, which writes the `PayPalEvent` node.

`paypal_replay.py` calls `store_paypal_event` as well, so a recorded delivery
goes through exactly the same code.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request
from pydantic import ValidationError

from app.deps import get_graph
from app.errors import ApiError
from app.graph.client import GraphClient
from app.ingest import service
from app.ingest.models import IngestResult, PayPalEventIn
from app.ingest.paypal_client import PayPalClient, PayPalError, normalise_headers

logger = logging.getLogger("app.paypal.webhook")

router = APIRouter(prefix="/ingest/paypal", tags=["paypal"])

#: The scope the Task 6 service uses for its idempotency keys.
IDEMPOTENCY_SCOPE = "paypal"


def get_paypal(request: Request) -> PayPalClient:
    """The one PayPal client, made at startup (see main.py)."""
    client = getattr(request.app.state, "paypal", None)
    if client is None:
        raise ApiError(503, "paypal_unavailable", "PayPal is not configured for this app.")
    return client


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ""


def _purchase_units(resource: dict[str, Any]) -> list[dict[str, Any]]:
    units = resource.get("purchase_units")
    if not isinstance(units, list):
        return []
    return [unit for unit in units if isinstance(unit, dict)]


def event_to_paypal_event(event: dict[str, Any]) -> PayPalEventIn:
    """Turn a PayPal webhook event into the row the ingest service expects.

    The field names come from PayPal's own documentation:

    * the event id is `id`;
    * the event name is `event_type`;
    * the time is `create_time`;
    * our end-to-end id is `custom_id`, either on the resource itself or on the
      first purchase unit of a checkout order;
    * the money is `amount.value` and `amount.currency_code`, on the resource
      (a capture) or on the first purchase unit (an order).
    """
    event_id = _text(event.get("id"))
    event_type = _text(event.get("event_type"))
    create_time = _text(event.get("create_time"))

    resource = event.get("resource")
    if not isinstance(resource, dict):
        resource = {}

    custom_id = _text(resource.get("custom_id"))
    units = _purchase_units(resource)
    if not custom_id:
        for unit in units:
            custom_id = _text(unit.get("custom_id"))
            if custom_id:
                break

    value = ""
    currency = ""
    for holder in (resource, *units):
        amount = holder.get("amount")
        if isinstance(amount, dict):
            value = _text(amount.get("value"))
            currency = _text(amount.get("currency_code")).upper()
            if value and currency:
                break

    missing = [
        name
        for name, field in (
            ("id", event_id),
            ("event_type", event_type),
            ("create_time", create_time),
            ("customId", custom_id),
            ("amount", value),
            ("currency", currency),
        )
        if not field
    ]
    if missing:
        raise ApiError(
            422, "missing_field", f"The PayPal event has no {', '.join(missing)}."
        )

    raw_ref = _text(resource.get("id")) or None
    try:
        return PayPalEventIn(
            eventId=event_id,
            type=event_type,
            ts=create_time,
            customId=custom_id,
            amount=value,
            currency=currency,
            rawRef=raw_ref,
        )
    except ValidationError as exc:
        raise ApiError(422, "missing_field", "The PayPal event is not usable.") from exc


async def store_paypal_event(
    graph: GraphClient,
    event: dict[str, Any],
    headers: dict[str, str] | None,
    verify: Callable[[dict[str, str] | None, dict[str, Any]], Awaitable[bool]],
) -> IngestResult:
    """Check the signature, drop a known event, then store the new one.

    `verify(headers, event)` returns True when PayPal says the delivery is
    genuine. The live route passes `PayPalClient.verify_webhook_signature`;
    the replay passes the local test verifier.
    """
    verified = await verify(headers, event)
    if not verified:
        raise ApiError(400, "invalid_signature", "PayPal did not confirm this webhook.")

    event_id = _text(event.get("id"))
    if not event_id:
        raise ApiError(422, "missing_field", "The PayPal event has no id.")

    # Drop an event id that was seen before. The Task 6 service keeps one
    # Idempotency node per event, so this is the same check it makes.
    seen = await graph.run_named(
        "get_idempotency", {"key": f"{IDEMPOTENCY_SCOPE}:{event_id}"}
    )
    if seen:
        logger.info("PayPal event %s was seen before and is dropped.", event_id)
        return IngestResult(id=event_id, created=False, duplicate=True)

    payload = event_to_paypal_event(event)
    return await service.ingest_paypal_event(graph, event_id, payload)


@router.post("/webhook", response_model=IngestResult)
async def post_paypal_webhook(
    request: Request,
    graph: Annotated[GraphClient, Depends(get_graph)],
    paypal: Annotated[PayPalClient, Depends(get_paypal)],
) -> IngestResult:
    """Take one PayPal webhook delivery."""
    try:
        event = await request.json()
    except ValueError:
        raise ApiError(400, "invalid_event", "The webhook body is not JSON.") from None
    if not isinstance(event, dict):
        raise ApiError(400, "invalid_event", "The webhook body is not a JSON object.")

    headers = normalise_headers(request.headers)
    try:
        return await store_paypal_event(graph, event, headers, paypal.verify_webhook_signature)
    except PayPalError as exc:
        # The reason is logged without the body, the headers or any secret.
        logger.warning("PayPal webhook refused: %s", type(exc).__name__)
        raise ApiError(
            400, "invalid_signature", "PayPal could not confirm this webhook."
        ) from exc
