"""Task 8: tests for the PayPal sandbox connection.

PayPal is mocked with `httpx.MockTransport`, so no test ever touches the
network and no test ever needs real credentials.

Covered here:

* a valid event is stored;
* a forged event is rejected with 400 and the graph does not change;
* the same event twice is stored once;
* startup refuses a production URL.
"""

import json
import logging
from pathlib import Path

import httpx
import pytest
import relate
from httpx import ASGITransport

from app.config import get_settings
from app.ingest.paypal_client import SANDBOX_BASE_URL, PayPalClient, PayPalConfigError
from app.ingest.paypal_replay import local_test_verifier, replay_recorded_events
from app.main import app

FIXTURES = Path(__file__).parent / "fixtures" / "paypal"

WEBHOOK_HEADERS = {
    "paypal-auth-algo": "SHA256withRSA",
    "paypal-cert-url": "https://api.paypal.com/v1/notifications/certs/CERT-sbx-recorded-0000000000000001",
    "paypal-transmission-id": "5f9a1c22-0b7d-4e6f-9a10-000000000001",
    "paypal-transmission-sig": "recorded-sandbox-transmission-signature-000000000001",
    "paypal-transmission-time": "2026-11-20T12:00:02Z",
}


def recorded_delivery(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


EVENT = recorded_delivery("01-checkout-order-approved.json")["event"]
DELIVERY = recorded_delivery("01-checkout-order-approved.json")


def paypal_mock(verification_status: str = "SUCCESS") -> PayPalClient:
    """A PayPal client that talks to a mock instead of the sandbox."""
    seen: dict = {}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        seen[path] = request
        if path == "/v1/oauth2/token":
            return httpx.Response(
                200,
                json={
                    "access_token": "sandbox-test-token",
                    "token_type": "Bearer",
                    "expires_in": 3600,
                },
            )
        if path == "/v1/notifications/verify-webhook-signature":
            return httpx.Response(200, json={"verification_status": verification_status})
        if path == "/v2/checkout/orders":
            return httpx.Response(
                201,
                json={"id": "5O190127TN364715T", "status": "CREATED", "intent": "CAPTURE"},
            )
        return httpx.Response(404, json={"name": "RESOURCE_NOT_FOUND"})

    client = PayPalClient(
        client_id="sandbox-client-id",
        client_secret="sandbox-client-secret",
        webhook_id="WH-TEST",
        transport=httpx.MockTransport(handler),
    )
    client.seen_requests = seen  # type: ignore[attr-defined]
    return client


def read_paypal_events() -> list[dict]:
    """Read the PayPalEvent nodes straight from the temporary graph.

    The app holds the store file while it runs, so this is only called after
    the app has stopped.
    """
    conn = relate.connect(get_settings().relate_path)
    try:
        cursor = conn.execute(
            "MATCH (e:PayPalEvent) RETURN e.eventId AS eventId, e.type AS type, "
            "e.customId AS customId, e.amount AS amount, e.currency AS currency, "
            "e.rawRef AS rawRef, e.ingestCount AS ingestCount"
        )
        return [row.data() for row in cursor.fetchmany(100)]
    finally:
        conn.close()


@pytest.fixture
def api_factory(app_env):
    """Start the app with a mocked PayPal and hand over an HTTP client."""

    async def start(client: PayPalClient):
        async with app.router.lifespan_context(app):
            app.state.paypal = client
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                yield http

    return start


# ---- a valid event is stored ----------------------------------------------


async def test_a_valid_event_is_stored(api_factory):
    async for http in api_factory(paypal_mock()):
        response = await http.post(
            "/ingest/paypal/webhook", json=EVENT, headers=WEBHOOK_HEADERS
        )
        assert response.status_code == 200
        assert response.json() == {"id": "WH-SBX-000001", "created": True, "duplicate": False}

    events = read_paypal_events()
    assert len(events) == 1
    assert events[0]["eventId"] == "WH-SBX-000001"
    assert events[0]["type"] == "CHECKOUT.ORDER.APPROVED"
    assert events[0]["customId"] == "E2E-SBX-0001"
    assert events[0]["amount"] == 125.5
    assert events[0]["currency"] == "USD"
    assert events[0]["rawRef"] == "5O190127TN364715T"
    assert events[0]["ingestCount"] == 1


async def test_the_signature_is_checked_before_anything_is_stored(api_factory):
    client = paypal_mock()
    async for http in api_factory(client):
        await http.post("/ingest/paypal/webhook", json=EVENT, headers=WEBHOOK_HEADERS)
    asked = client.seen_requests.get("/v1/notifications/verify-webhook-signature")
    assert asked is not None
    body = json.loads(asked.content)
    assert body["webhook_id"] == "WH-TEST"
    assert body["transmission_id"] == WEBHOOK_HEADERS["paypal-transmission-id"]
    assert body["transmission_sig"] == WEBHOOK_HEADERS["paypal-transmission-sig"]
    assert body["auth_algo"] == WEBHOOK_HEADERS["paypal-auth-algo"]
    assert body["cert_url"] == WEBHOOK_HEADERS["paypal-cert-url"]
    assert body["webhook_event"]["id"] == "WH-SBX-000001"


# ---- a forged event changes nothing ---------------------------------------


async def test_a_forged_event_is_rejected_with_400(api_factory):
    async for http in api_factory(paypal_mock(verification_status="FAILURE")):
        response = await http.post(
            "/ingest/paypal/webhook", json=EVENT, headers=WEBHOOK_HEADERS
        )
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_signature"

    assert read_paypal_events() == []


async def test_a_forged_event_without_the_transmission_headers_is_rejected(api_factory):
    async for http in api_factory(paypal_mock()):
        response = await http.post("/ingest/paypal/webhook", json=EVENT)
        assert response.status_code == 400
        assert response.json()["error"] == "invalid_signature"

    assert read_paypal_events() == []


async def test_a_changed_event_body_is_rejected_with_400(api_factory):
    forged = dict(EVENT)
    forged["resource"] = {**EVENT["resource"], "custom_id": "E2E-FORGED-9999"}
    async for http in api_factory(paypal_mock(verification_status="FAILURE")):
        response = await http.post(
            "/ingest/paypal/webhook", json=forged, headers=WEBHOOK_HEADERS
        )
        assert response.status_code == 400

    assert read_paypal_events() == []


# ---- the same event twice is stored once ----------------------------------


async def test_the_same_event_twice_is_stored_once(api_factory):
    async for http in api_factory(paypal_mock()):
        first = await http.post(
            "/ingest/paypal/webhook", json=EVENT, headers=WEBHOOK_HEADERS
        )
        second = await http.post(
            "/ingest/paypal/webhook", json=EVENT, headers=WEBHOOK_HEADERS
        )
        assert first.json()["created"] is True
        assert second.json() == {"id": "WH-SBX-000001", "created": False, "duplicate": True}

    events = read_paypal_events()
    assert len(events) == 1
    assert events[0]["ingestCount"] == 1


# ---- the app refuses to start against production --------------------------


def test_the_client_refuses_a_production_url():
    with pytest.raises(PayPalConfigError):
        PayPalClient(base_url="https://api-m.paypal.com")


def test_the_client_refuses_a_look_alike_host():
    with pytest.raises(PayPalConfigError):
        PayPalClient(base_url="https://api-m.sandbox.paypal.com.example.org")


async def test_startup_refuses_a_production_url(app_env, monkeypatch):
    monkeypatch.setenv("PAYPAL_BASE_URL", "https://api-m.paypal.com")
    with pytest.raises(PayPalConfigError):
        async with app.router.lifespan_context(app):
            pass


async def test_startup_is_happy_with_the_sandbox(app_env, monkeypatch):
    monkeypatch.setenv("PAYPAL_BASE_URL", SANDBOX_BASE_URL)
    async with app.router.lifespan_context(app):
        assert app.state.paypal.base_url == SANDBOX_BASE_URL


# ---- creating an order ----------------------------------------------------


async def test_a_sandbox_payment_produces_a_paypal_event(api_factory):
    """The order carries our end-to-end id and the event carries it back.

    That shared id is what Task 9 matches on, so the two halves have to agree.
    """
    client = paypal_mock()
    async for http in api_factory(client):
        order = await client.create_order("125.50", "USD", "E2E-SBX-0001")
        assert order["id"] == "5O190127TN364715T"

        response = await http.post(
            "/ingest/paypal/webhook", json=EVENT, headers=WEBHOOK_HEADERS
        )
        assert response.status_code == 200
        assert response.json()["created"] is True

    events = read_paypal_events()
    assert len(events) == 1
    assert events[0]["customId"] == "E2E-SBX-0001"
    assert events[0]["amount"] == 125.5
    assert events[0]["currency"] == "USD"


async def test_create_order_puts_the_end_to_end_id_in_the_custom_field():
    client = paypal_mock()
    await client.create_order("125.50", "USD", "E2E-SBX-0001")
    request = client.seen_requests.get("/v2/checkout/orders")
    assert request is not None
    body = json.loads(request.content)
    assert body["intent"] == "CAPTURE"
    assert body["purchase_units"][0]["custom_id"] == "E2E-SBX-0001"
    assert body["purchase_units"][0]["amount"] == {"currency_code": "USD", "value": "125.50"}


async def test_the_access_token_is_never_written_to_the_log(app_env, caplog):
    client = paypal_mock()
    with caplog.at_level(logging.DEBUG):
        await client.get_access_token()
    assert "sandbox-client-secret" not in caplog.text
    assert "sandbox-test-token" not in caplog.text


async def test_no_secret_reaches_the_log_while_a_webhook_is_handled(app_env, caplog):
    with caplog.at_level(logging.DEBUG):
        async with app.router.lifespan_context(app):
            app.state.paypal = paypal_mock()
            transport = ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http:
                await http.post(
                    "/ingest/paypal/webhook", json=EVENT, headers=WEBHOOK_HEADERS
                )
    assert "sandbox-client-secret" not in caplog.text
    assert "sandbox-test-token" not in caplog.text


# ---- the replay fallback ---------------------------------------------------


async def test_the_replay_goes_through_the_same_code_path(app_env):
    async with app.router.lifespan_context(app):
        results = await replay_recorded_events(app.state.graph)

    assert len(results) == 3
    assert {result.id for result in results} == {
        "WH-SBX-000001",
        "WH-SBX-000002",
        "WH-SBX-000003",
    }
    events = read_paypal_events()
    assert len(events) == 3
    assert {event["eventId"] for event in events} == {
        "WH-SBX-000001",
        "WH-SBX-000002",
        "WH-SBX-000003",
    }


async def test_the_replay_is_refused_outside_local_demo_mode(app_env, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "demo")
    monkeypatch.setenv("JWT_SECRET", "a-long-test-only-secret")
    async with app.router.lifespan_context(app):
        with pytest.raises(RuntimeError):
            await replay_recorded_events(app.state.graph)


async def test_the_local_verifier_only_accepts_a_recorded_delivery(app_env):
    assert await local_test_verifier(DELIVERY["headers"], DELIVERY["event"]) is True
    assert await local_test_verifier(DELIVERY["headers"], {"id": "WH-NOT-RECORDED"}) is False


async def test_the_local_verifier_needs_the_transmission_headers(app_env):
    assert await local_test_verifier({}, DELIVERY["event"]) is False


async def test_the_local_verifier_is_off_outside_local_demo_mode(app_env, monkeypatch):
    monkeypatch.setenv("DEMO_MODE", "demo")
    assert await local_test_verifier(DELIVERY["headers"], DELIVERY["event"]) is False
