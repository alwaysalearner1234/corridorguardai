"""Task 7: tests for the ISO 20022 message reader.

The three valid files are copies of what the generator makes. The three attack
files are the ones that must never reach the parser.
"""

from pathlib import Path

import httpx
import pytest
from httpx import ASGITransport

from app.errors import ApiError
from app.ingest.iso20022 import MAX_XML_BYTES, parse_message, parse_message_fields
from app.main import app

XML_DIR = Path(__file__).parent / "fixtures" / "xml"

UETR = "aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee"
END_TO_END_ID = "E2E-XML-0001"


def read(name: str) -> bytes:
    return (XML_DIR / name).read_bytes()


@pytest.fixture
async def api(app_env):
    async with app.router.lifespan_context(app):
        transport = ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
            yield client


# ---- the three valid message types ----------------------------------------


def test_pacs008_parses_to_the_expected_fields():
    parsed = parse_message_fields(read("pacs008.xml"))
    assert parsed.type == "pacs.008"
    assert parsed.msgId == "M-900001"
    assert parsed.ts.isoformat() == "2026-11-20T10:00:00+00:00"
    assert parsed.uetr == UETR
    assert parsed.endToEndId == END_TO_END_ID
    assert str(parsed.amount) == "1250.50"
    assert parsed.currency == "USD"
    assert parsed.debtor == "Callum Yardley"
    assert parsed.creditor == "Soren Zielinski"
    assert parsed.status == "SENT"


def test_pacs002_parses_to_the_expected_fields():
    parsed = parse_message_fields(read("pacs002.xml"))
    assert parsed.type == "pacs.002"
    assert parsed.msgId == "M-900002"
    assert parsed.uetr == UETR
    assert parsed.endToEndId == END_TO_END_ID
    assert str(parsed.amount) == "1250.50"
    assert parsed.currency == "USD"
    assert parsed.status == "ACSC"


def test_camt053_parses_to_the_expected_fields():
    parsed = parse_message_fields(read("camt053.xml"))
    assert parsed.type == "camt.053"
    assert parsed.msgId == "M-900003"
    assert parsed.uetr == UETR
    assert parsed.endToEndId == END_TO_END_ID
    assert str(parsed.amount) == "1228.75"
    assert parsed.currency == "USD"
    assert parsed.creditor == "Soren Zielinski"
    assert parsed.status == "BOOK"


def test_parse_message_gives_the_row_the_service_wants():
    row = parse_message(read("pacs008.xml"))
    assert row.type == "pacs.008"
    assert row.uetr == UETR
    assert row.endToEndId == END_TO_END_ID
    assert str(row.amount) == "1250.50"
    assert row.currency == "USD"
    assert row.xml is not None and row.xml.startswith("<Document")


# ---- the attack files ------------------------------------------------------


def test_xxe_is_refused_as_unsafe_xml():
    with pytest.raises(ApiError) as caught:
        parse_message(read("xxe.xml"))
    assert caught.value.code == "unsafe_xml"
    assert caught.value.status_code == 400


def test_billion_laughs_is_refused_as_unsafe_xml():
    with pytest.raises(ApiError) as caught:
        parse_message(read("billion_laughs.xml"))
    assert caught.value.code == "unsafe_xml"


def test_a_message_over_the_cap_is_refused_before_parsing():
    raw = read("oversized.xml")
    assert len(raw) > MAX_XML_BYTES
    with pytest.raises(ApiError) as caught:
        parse_message(raw)
    assert caught.value.code == "too_large"
    assert caught.value.status_code == 413


def test_the_cap_is_256_kb():
    assert MAX_XML_BYTES == 262_144


# ---- missing and broken fields --------------------------------------------


def test_a_message_without_a_uetr_says_missing_field():
    raw = read("pacs008.xml").replace(
        b"<UETR>aaaaaaaa-bbbb-4ccc-8ddd-eeeeeeeeeeee</UETR>", b""
    )
    with pytest.raises(ApiError) as caught:
        parse_message(raw)
    assert caught.value.code == "missing_field"
    assert "uetr" in caught.value.message


def test_a_message_without_an_end_to_end_id_says_missing_field():
    raw = read("pacs008.xml").replace(b"<EndToEndId>E2E-XML-0001</EndToEndId>", b"")
    with pytest.raises(ApiError) as caught:
        parse_message(raw)
    assert caught.value.code == "missing_field"
    assert "endToEndId" in caught.value.message


def test_an_unknown_namespace_is_refused():
    raw = read("pacs008.xml").replace(b"pacs.008.001.08", b"pain.001.001.09")
    with pytest.raises(ApiError) as caught:
        parse_message(raw)
    assert caught.value.code == "invalid_message"


def test_a_body_that_is_not_xml_is_refused():
    with pytest.raises(ApiError) as caught:
        parse_message(b"this is not xml at all")
    assert caught.value.code == "unsafe_xml"


# ---- the route -------------------------------------------------------------


async def test_the_xml_route_stores_a_bank_message(api):
    response = await api.post(
        "/ingest/bank-message-xml",
        content=read("pacs008.xml"),
        headers={"Idempotency-Key": "xml-1", "Content-Type": "application/xml"},
    )
    assert response.status_code == 200
    assert response.json() == {"id": "M-900001", "created": True, "duplicate": False}

    links = await app.state.graph.run_named("get_payment_links", {"uetr": UETR})
    assert [row["labels"] for row in links] == [["BankMessage"]]


async def test_the_xml_route_needs_an_idempotency_key(api):
    response = await api.post(
        "/ingest/bank-message-xml",
        content=read("pacs008.xml"),
        headers={"Content-Type": "application/xml"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "idempotency_key_required"


async def test_the_xml_route_refuses_an_attack_file_and_stores_nothing(api):
    response = await api.post(
        "/ingest/bank-message-xml",
        content=read("xxe.xml"),
        headers={"Idempotency-Key": "xml-xxe", "Content-Type": "application/xml"},
    )
    assert response.status_code == 400
    assert response.json()["error"] == "unsafe_xml"

    payment = await app.state.graph.run_named("get_payment", {"uetr": UETR})
    assert payment == []


async def test_the_xml_route_refuses_a_message_over_the_cap(api):
    response = await api.post(
        "/ingest/bank-message-xml",
        content=read("oversized.xml"),
        headers={"Idempotency-Key": "xml-big", "Content-Type": "application/xml"},
    )
    assert response.status_code == 413
    assert response.json()["error"] == "too_large"


async def test_the_same_xml_twice_is_stored_once(api):
    first = await api.post(
        "/ingest/bank-message-xml",
        content=read("pacs002.xml"),
        headers={"Idempotency-Key": "xml-2", "Content-Type": "application/xml"},
    )
    second = await api.post(
        "/ingest/bank-message-xml",
        content=read("pacs002.xml"),
        headers={"Idempotency-Key": "xml-3", "Content-Type": "application/xml"},
    )
    assert first.json()["created"] is True
    assert second.json() == {"id": "M-900002", "created": False, "duplicate": True}
