"""Task 8: replay recorded PayPal sandbox events.

The fallback for a demo without live PayPal. The deliveries recorded in
`backend/tests/fixtures/paypal/*.json` go through the same code as a live
webhook, including a signature check.

The check for a recorded delivery is `local_test_verifier`. It only works when
`DEMO_MODE=local`, because it stands in for PayPal's own signature check: it
accepts a delivery only when it is one of the recorded sandbox deliveries and
it carries the transmission headers PayPal sent with it. Outside local demo
mode it refuses, because a local check is not a real signature check.

The recorded signatures are not real PayPal signatures. Only PayPal's verify
endpoint can confirm a live delivery.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

from app.errors import ApiError
from app.graph.client import GraphClient
from app.ingest.models import IngestResult
from app.ingest.paypal_client import normalise_headers
from app.ingest.paypal_webhook import store_paypal_event

logger = logging.getLogger("app.paypal.replay")

#: The recorded deliveries.
FIXTURES_DIR = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "paypal"


def local_demo_mode() -> bool:
    """True only when DEMO_MODE is exactly 'local'."""
    return (os.getenv("DEMO_MODE") or "").strip().lower() == "local"


def recorded_deliveries(directory: Path | None = None) -> list[dict[str, Any]]:
    """Every recorded delivery in the fixtures folder, in file name order.

    A delivery is a JSON object with two keys: `headers` (the transmission
    headers PayPal sent) and `event` (the event PayPal sent).
    """
    folder = directory or FIXTURES_DIR
    if not folder.is_dir():
        return []
    deliveries: list[dict[str, Any]] = []
    for path in sorted(folder.glob("*.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            continue
        deliveries.append(data)
    return deliveries


async def local_test_verifier(headers: dict[str, str] | None, event: dict[str, Any]) -> bool:
    """The local verifier for replayed deliveries. Local demo mode only.

    It stands in for `PayPalClient.verify_webhook_signature`: the delivery has
    to be one of the recorded sandbox deliveries and it has to carry the
    transmission headers PayPal sent with it.
    """
    if not local_demo_mode():
        logger.warning("Replay refused: DEMO_MODE is not 'local'.")
        return False

    sent = normalise_headers(headers)
    required = (
        "paypal-transmission-id",
        "paypal-transmission-sig",
        "paypal-transmission-time",
        "paypal-auth-algo",
        "paypal-cert-url",
    )
    if any(not sent.get(name) for name in required):
        return False

    event_id = str(event.get("id") or "")
    for delivery in recorded_deliveries():
        recorded_event = delivery.get("event")
        if not isinstance(recorded_event, dict):
            continue
        if str(recorded_event.get("id") or "") != event_id:
            continue
        recorded_headers = normalise_headers(delivery.get("headers"))
        return (
            bool(recorded_headers.get("paypal-transmission-id"))
            and recorded_headers.get("paypal-transmission-id")
            == sent.get("paypal-transmission-id")
        )
    return False


async def replay_recorded_events(
    graph: GraphClient, directory: Path | None = None
) -> list[IngestResult]:
    """Replay every recorded delivery through the webhook code path.

    The signature check is not skipped: `local_test_verifier` stands in for
    PayPal's own check, and it only works in local demo mode.
    """
    if not local_demo_mode():
        raise RuntimeError(
            "Replay only works when DEMO_MODE=local. Outside local demo mode "
            "a recorded delivery cannot be checked honestly."
        )

    results: list[IngestResult] = []
    for delivery in recorded_deliveries(directory):
        event = delivery.get("event")
        headers = delivery.get("headers")
        if not isinstance(event, dict) or not isinstance(headers, dict):
            logger.warning("A recorded delivery is not shaped like a delivery, skipped.")
            continue
        try:
            result = await store_paypal_event(graph, event, headers, local_test_verifier)
        except ApiError as exc:
            logger.warning("A recorded delivery was refused: %s", exc.code)
            continue
        results.append(result)
    return results


async def _main() -> None:
    """Replay the recorded deliveries into the graph. `python -m app.ingest.paypal_replay`."""
    from app.config import get_settings
    from app.graph.schema import apply_schema

    settings = get_settings()
    client = GraphClient(
        settings.relate_path,
        query_dir=settings.graph_query_dir,
        default_limit=settings.graph_row_limit,
        default_timeout_s=settings.graph_timeout_s,
    )
    try:
        await apply_schema(client)
        for result in await replay_recorded_events(client):
            print(result.model_dump_json())
    finally:
        await client.close()


if __name__ == "__main__":
    import asyncio

    asyncio.run(_main())
