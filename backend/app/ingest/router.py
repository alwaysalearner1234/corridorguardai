import re
from typing import Annotated

from fastapi import APIRouter, Depends, Header, Request

from app.deps import get_graph, require_service_role
from app.errors import ApiError
from app.graph.client import GraphClient
from app.ingest import service
from app.ingest.iso20022 import parse_message
from app.ingest.models import (
    BankMessageIn,
    IngestResult,
    LedgerEntryIn,
    PaymentIn,
    PayPalEventIn,
)

router = APIRouter(
    prefix="/ingest", tags=["ingest"], dependencies=[Depends(require_service_role)]
)

_KEY = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def idempotency_key(
    idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
) -> str:
    if not idempotency_key:
        raise ApiError(400, "idempotency_key_required", "Header Idempotency-Key is required.")
    if not _KEY.fullmatch(idempotency_key):
        raise ApiError(
            400, "idempotency_key_invalid", "Idempotency-Key must be 1-128 letters, digits . _ : -"
        )
    return idempotency_key


Key = Annotated[str, Depends(idempotency_key)]
Graph = Annotated[GraphClient, Depends(get_graph)]


@router.post("/payment", response_model=IngestResult)
async def post_payment(body: PaymentIn, key: Key, graph: Graph) -> IngestResult:
    return await service.ingest_payment(graph, key, body)


@router.post("/ledger-entry", response_model=IngestResult)
async def post_ledger_entry(body: LedgerEntryIn, key: Key, graph: Graph) -> IngestResult:
    return await service.ingest_ledger_entry(graph, key, body)


@router.post("/paypal-event", response_model=IngestResult)
async def post_paypal_event(body: PayPalEventIn, key: Key, graph: Graph) -> IngestResult:
    return await service.ingest_paypal_event(graph, key, body)


@router.post("/bank-message", response_model=IngestResult)
async def post_bank_message(body: BankMessageIn, key: Key, graph: Graph) -> IngestResult:
    return await service.ingest_bank_message(graph, key, body)


@router.post("/bank-message-xml", response_model=IngestResult)
async def post_bank_message_xml(request: Request, key: Key, graph: Graph) -> IngestResult:
    """Read a raw ISO 20022 message (pacs.008, pacs.002 or camt.053).

    The body is XML, not JSON. It is capped, parsed with defusedxml and read by
    `parse_message`; then the Task 6 ingest service stores it.
    """
    message = parse_message(await request.body())
    return await service.ingest_bank_message(graph, key, message)