"""Task 7: ISO 20022 message reader.

Turns a raw bank message (pacs.008, pacs.002 or camt.053) into a `BankMessageIn`
row, which the Task 6 ingest service writes as a `BankMessage` node.

Safety, in this order:

1. the body is checked against a 256 KB cap before anything is parsed;
2. the XML is parsed only with defusedxml, with DTDs, entities and external
   references switched off;
3. the parse itself runs under a time budget;
4. every field the graph needs is checked, and a missing one is named.

Error codes raised from here:

* ``too_large`` - the body is bigger than 256 KB;
* ``unsafe_xml`` - the body uses a DTD, an entity or an external reference, or
  cannot be parsed safely at all;
* ``invalid_message`` - the XML is not one of the three supported message types,
  or a value is not usable;
* ``missing_field`` - a field the graph needs is not in the message; the message
  text names the field.
"""

from __future__ import annotations

import re
from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from xml.etree.ElementTree import Element, ParseError

from defusedxml import ElementTree as SafeET
from defusedxml.common import DefusedXmlException
from pydantic import ValidationError

from app.errors import ApiError
from app.ingest.models import BankMessageIn

# 256 KB. The BankMessage node keeps the raw XML, so this is also the size of
# the `xml` property on the graph.
MAX_XML_BYTES = 256 * 1024

# Budget for one parse. With DTDs and entities switched off a small document
# cannot take long, but the budget keeps a pathological body from holding a
# worker thread for ever.
PARSE_TIMEOUT_S = 2.0

# The ISO 20022 namespaces the reader understands, matched loosely so that
# different versions of the same message (pacs.008.001.08, pacs.008.001.10 ...)
# still work.
_NS_TYPE = re.compile(r"iso:20022:tech:xsd:(pacs\.008|pacs\.002|camt\.053)", re.IGNORECASE)

# Per message type: where the amount and the status live, what the status is
# when the message does not carry one, and where the two parties live.
TYPE_RULES: dict[str, dict[str, object]] = {
    "pacs.008": {
        "amount": ("IntrBkSttlmAmt", "Amt"),
        "status": ("TxSts", "Sts"),
        "status_default": "SENT",
        "debtor": ("Dbtr", "DbtrAcct"),
        "creditor": ("Cdtr", "CdtrAcct"),
    },
    "pacs.002": {
        "amount": ("IntrBkSttlmAmt", "Amt"),
        "status": ("TxSts", "Sts"),
        "status_default": None,
        "debtor": ("Dbtr", "DbtrAcct"),
        "creditor": ("Cdtr", "CdtrAcct"),
    },
    "camt.053": {
        "amount": ("Amt", "IntrBkSttlmAmt"),
        "status": ("Sts", "TxSts"),
        "status_default": "BOOK",
        "debtor": ("Dbtr", "DbtrAcct"),
        "creditor": ("Cdtr", "CdtrAcct"),
    },
}

# The fields the graph cannot do without.
REQUIRED_FIELDS = ("msgId", "ts", "uetr", "endToEndId", "amount", "currency")

_PARSER = ThreadPoolExecutor(max_workers=2, thread_name_prefix="xmlparse")


@dataclass(frozen=True)
class ParsedMessage:
    """Every field the reader pulls out of one message."""

    type: str
    msgId: str
    ts: datetime
    status: str | None
    uetr: str
    endToEndId: str
    amount: Decimal
    currency: str
    debtor: str | None
    creditor: str | None
    xml: str


def _local(tag: object) -> str:
    """The tag without its namespace: '{urn:...}MsgId' -> 'MsgId'."""
    if not isinstance(tag, str):
        return ""
    if tag.startswith("{"):
        return tag.split("}", 1)[1]
    return tag


def _namespace(tag: object) -> str:
    """The namespace of a tag: '{urn:...}MsgId' -> 'urn:...'."""
    if not isinstance(tag, str) or not tag.startswith("{"):
        return ""
    return tag[1:].split("}", 1)[0]


def _text(element: Element | None) -> str:
    if element is None:
        return ""
    return "".join(element.itertext()).strip()


def _index(root: Element) -> dict[str, list[Element]]:
    """Every element of the document, grouped by local tag name."""
    found: dict[str, list[Element]] = {}
    for element in root.iter():
        found.setdefault(_local(element.tag), []).append(element)
    return found


def _descendant(element: Element, local: str) -> Element | None:
    """The first descendant of `element` with the given local tag name."""
    for child in element.iter():
        if child is not element and _local(child.tag) == local:
            return child
    return None


def _pick(index: dict[str, list[Element]], names: tuple[str, ...]) -> str:
    """The text of the first element that carries one of the given names."""
    for name in names:
        for element in index.get(name, []):
            text = _text(element)
            if text:
                return text
    return ""


def _pick_element(
    index: dict[str, list[Element]], names: tuple[str, ...]
) -> tuple[Element | None, str]:
    for name in names:
        for element in index.get(name, []):
            text = _text(element)
            if text:
                return element, text
    return None, ""


def _party_name(index: dict[str, list[Element]], names: tuple[str, ...]) -> str | None:
    """The name of the debtor or the creditor, if the message carries one."""
    for name in names:
        for element in index.get(name, []):
            named = _descendant(element, "Nm")
            text = _text(named) if named is not None else _text(element)
            if text:
                return text
    return None


def _parse(raw: bytes) -> Element:
    """Parse the bytes with defusedxml, under a time budget."""
    future = _PARSER.submit(
        SafeET.fromstring,
        raw,
        forbid_dtd=True,
        forbid_entities=True,
        forbid_external=True,
    )
    try:
        return future.result(timeout=PARSE_TIMEOUT_S)
    except FutureTimeout as exc:
        future.cancel()
        raise ApiError(
            400, "unsafe_xml", "The XML took too long to parse and was refused."
        ) from exc
    except DefusedXmlException as exc:
        # DTDs, entity declarations and external references all land here.
        raise ApiError(
            400,
            "unsafe_xml",
            "The XML uses a DTD, an entity or an external reference and was refused.",
        ) from exc
    except ParseError as exc:
        raise ApiError(
            400, "unsafe_xml", "The body is not XML that can be parsed safely."
        ) from exc
    except ValueError as exc:
        # Bytes that are not text at all.
        raise ApiError(
            400, "unsafe_xml", "The body is not XML that can be parsed safely."
        ) from exc


def _detect_type(root: Element, index: dict[str, list[Element]]) -> str:
    """The message type, taken from the XML namespace."""
    tags = [root.tag, *(element.tag for element in root.iter())]
    for tag in tags:
        found = _NS_TYPE.search(_namespace(tag))
        if found:
            return found.group(1).lower()
    raise ApiError(
        400,
        "invalid_message",
        "The XML namespace is not pacs.008, pacs.002 or camt.053.",
    )


def _first_validation_message(exc: ValidationError) -> str:
    error = exc.errors()[0]
    where = ".".join(str(part) for part in error["loc"]) or "body"
    return f"{where}: {error['msg']}"


def parse_message_fields(xml_bytes: bytes) -> ParsedMessage:
    """Read every field of one ISO 20022 message.

    Raises `ApiError` with `too_large`, `unsafe_xml`, `invalid_message` or
    `missing_field`.
    """
    raw = xml_bytes.encode("utf-8") if isinstance(xml_bytes, str) else bytes(xml_bytes)
    if len(raw) > MAX_XML_BYTES:
        raise ApiError(
            413,
            "too_large",
            f"The message is bigger than the {MAX_XML_BYTES} byte limit.",
        )

    root = _parse(raw)
    index = _index(root)
    msg_type = _detect_type(root, index)
    rules = TYPE_RULES[msg_type]

    msg_id = _pick(index, ("MsgId",))
    ts_text = _pick(index, ("CreDtTm", "CreDtTmWrk"))
    uetr = _pick(index, ("UETR", "OrgnlUETR"))
    end_to_end_id = _pick(index, ("EndToEndId", "OrgnlEndToEndId"))
    amount_element, amount_text = _pick_element(index, rules["amount"])  # type: ignore[arg-type]

    currency = ""
    if amount_element is not None:
        currency = (amount_element.get("Ccy") or amount_element.get("ccy") or "").strip().upper()

    for name, value in (
        ("msgId", msg_id),
        ("ts", ts_text),
        ("uetr", uetr),
        ("endToEndId", end_to_end_id),
        ("amount", amount_text),
        ("currency", currency),
    ):
        if not value:
            raise ApiError(422, "missing_field", f"The message has no {name}.")

    try:
        amount = Decimal(amount_text)
    except InvalidOperation as exc:
        raise ApiError(400, "invalid_message", "The amount is not a number.") from exc
    if not amount.is_finite():
        raise ApiError(400, "invalid_message", "The amount is not a number.")

    try:
        timestamp = datetime.fromisoformat(ts_text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "invalid_message", "The message time is not a timestamp.") from exc
    if timestamp.tzinfo is None:
        timestamp = timestamp.replace(tzinfo=UTC)

    status = _pick(index, rules["status"]) or rules["status_default"]  # type: ignore[arg-type]
    debtor = _party_name(index, rules["debtor"])  # type: ignore[arg-type]
    creditor = _party_name(index, rules["creditor"])  # type: ignore[arg-type]

    return ParsedMessage(
        type=msg_type,
        msgId=msg_id,
        ts=timestamp,
        status=status or None,
        uetr=uetr,
        endToEndId=end_to_end_id,
        amount=amount,
        currency=currency,
        debtor=debtor,
        creditor=creditor,
        xml=raw.decode("utf-8", errors="replace"),
    )


def parse_message(xml_bytes: bytes) -> BankMessageIn:
    """Read one ISO 20022 message into the row the ingest service expects."""
    parsed = parse_message_fields(xml_bytes)
    try:
        return BankMessageIn(
            msgId=parsed.msgId,
            type=parsed.type,
            ts=parsed.ts,
            status=parsed.status,
            uetr=parsed.uetr,
            endToEndId=parsed.endToEndId,
            amount=parsed.amount,
            currency=parsed.currency,
            xml=parsed.xml,
        )
    except ValidationError as exc:
        raise ApiError(400, "invalid_message", _first_validation_message(exc)) from exc
