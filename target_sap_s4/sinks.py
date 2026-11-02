"""Standard S/4HANA synchronous journal-entry posting."""

from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from decimal import Decimal
from xml.etree import ElementTree as ET

from jsonschema import validate

from target_sap_s4.client import SapS4RecordSink

SOAP = "http://schemas.xmlsoap.org/soap/envelope/"
SFIN = "http://sap.com/xi/SAPSCORE/SFIN"
ET.register_namespace("soapenv", SOAP)
ET.register_namespace("sfin", SFIN)

ITEM_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["gl_account", "amount", "currency", "debit_credit_code"],
    "properties": {
        "gl_account": {"type": "string", "minLength": 1, "maxLength": 10},
        "amount": {"type": "string", "pattern": r"^-?\d+(\.\d+)?$"},
        "currency": {"type": "string", "pattern": "^[A-Z]{3}$"},
        "debit_credit_code": {"enum": ["S", "H"]},
        "item_text": {"type": "string", "maxLength": 50},
        "assignment_reference": {"type": "string", "maxLength": 18},
        "reference_document_item": {"type": "string", "pattern": "^[0-9]{1,6}$"},
        "account_assignment_type": {"enum": ["EO"]},
        "profit_center": {"type": "string", "maxLength": 10},
        "cost_center": {"type": "string", "maxLength": 10},
    },
}
JOURNAL_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["externalId", "company_code", "document_date", "posting_date", "items"],
    "properties": {
        "externalId": {"type": "string", "minLength": 1},
        "id": {"type": "string", "minLength": 1},
        "company_code": {"type": "string", "minLength": 1, "maxLength": 4},
        "document_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "posting_date": {"type": "string", "pattern": r"^\d{4}-\d{2}-\d{2}$"},
        "document_type": {"type": "string", "minLength": 1, "maxLength": 2},
        "header_text": {"type": "string", "maxLength": 25},
        "items": {"type": "array", "minItems": 2, "maxItems": 999, "items": ITEM_SCHEMA},
    },
}


class JournalEntriesSink(SapS4RecordSink):
    """Validate, simulate or create an immutable balanced accounting document."""

    name = "journal_entries"
    endpoint = "/sap/bc/srt/scs/sap/journalentrycreaterequestconfi"
    allows_externalid = [name]

    def preprocess_record(self, record, context):
        """Validate before sending; make simulation part of the SDK's record hash."""
        validate(record, JOURNAL_SCHEMA)
        for field in ("document_date", "posting_date"):
            date.fromisoformat(record[field])
        currencies = {item["currency"] for item in record["items"]}
        if len(currencies) != 1:
            raise ValueError("All journal items must use the same transaction currency")
        total = Decimal(0)
        for item in record["items"]:
            amount = Decimal(item["amount"])
            if not amount or (amount > 0) != (item["debit_credit_code"] == "S"):
                raise ValueError(
                    "Use positive nonzero debit S and negative nonzero credit H amounts"
                )
            total += amount
        if total != 0:
            raise ValueError("Journal entry is not balanced")
        prepared = dict(record)
        prepared["test_data_indicator"] = self.config.get("test_data_indicator", True)
        prepared["destination"] = [self.base_url, self.config.get("sap_client")]
        identity = f"{record['company_code']}:{record['externalId']}"
        prepared["document_reference"] = "HG" + hashlib.sha256(identity.encode()).hexdigest()[:14]
        return prepared

    def to_xml(self, record):
        """Serialize native SAP field order and XML-escape all tenant input."""
        envelope = ET.Element(f"{{{SOAP}}}Envelope")
        ET.SubElement(envelope, f"{{{SOAP}}}Header")
        body = ET.SubElement(envelope, f"{{{SOAP}}}Body")
        bulk = ET.SubElement(body, f"{{{SFIN}}}JournalEntryBulkCreateRequest")
        now = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")

        def text(parent, key, value):
            ET.SubElement(parent, key).text = str(value)

        def header(parent):
            node = ET.SubElement(parent, "MessageHeader")
            text(node, "CreationDateTime", now)
            text(node, "TestDataIndicator", str(record["test_data_indicator"]).lower())

        header(bulk)
        request = ET.SubElement(bulk, "JournalEntryCreateRequest")
        header(request)
        journal = ET.SubElement(request, "JournalEntry")
        for key, value in (
            ("OriginalReferenceDocumentType", "BKPFF"),
            ("BusinessTransactionType", "RFBU"),
            ("AccountingDocumentType", record.get("document_type", "SA")),
            ("DocumentReferenceID", record["document_reference"]),
            ("DocumentHeaderText", record.get("header_text", "Hotglue recovery")),
            ("CreatedByUser", self.config["username"]),
            ("CompanyCode", record["company_code"]),
            ("DocumentDate", record["document_date"]),
            ("PostingDate", record["posting_date"]),
        ):
            text(journal, key, value)
        for index, line in enumerate(record["items"], 1):
            item = ET.SubElement(journal, "Item")
            text(item, "ReferenceDocumentItem", line.get("reference_document_item", str(index)))
            text(item, "GLAccount", line["gl_account"])
            amount = ET.SubElement(
                item, "AmountInTransactionCurrency", currencyCode=line["currency"]
            )
            amount.text = line["amount"]
            text(item, "DebitCreditCode", line["debit_credit_code"])
            for key, field in (
                ("DocumentItemText", "item_text"),
                ("AssignmentReference", "assignment_reference"),
            ):
                if field in line:
                    text(item, key, line[field])
            if any(
                line.get(k) for k in ("account_assignment_type", "profit_center", "cost_center")
            ):
                assignment = ET.SubElement(item, "AccountAssignment")
                for key, field in (
                    ("AccountAssignmentType", "account_assignment_type"),
                    ("ProfitCenter", "profit_center"),
                    ("CostCenter", "cost_center"),
                ):
                    if line.get(field):
                        text(assignment, key, line[field])
        return ET.tostring(envelope, encoding="utf-8", xml_declaration=True)

    def upsert_record(self, record, context):
        """Send once and inspect SAP business results, not only HTTP success."""
        if record.get("id"):
            raise ValueError(
                "Existing financial documents cannot be overwritten; reconcile prior state"
            )
        if not record["test_data_indicator"]:
            # A lost SOAP response must not result in a duplicate on the next attempt.
            # No automatic reconciliation by amount/name: block for an explicit decision.
            response = self.send(
                "GET",
                "/sap/opu/odata/sap/API_OPLACCTGDOCITEMCUBE_SRV/A_OperationalAcctgDocItemCube",
                params={
                    "$format": "json",
                    "$top": "1",
                    "$select": "CompanyCode,FiscalYear,AccountingDocument",
                    "$filter": "CompanyCode eq '{}' and DocumentReferenceID eq '{}'".format(
                        record["company_code"].replace("'", "''"), record["document_reference"]
                    ),
                },
            )
            result = response.json()["d"]
            rows = result["results"]
            if not isinstance(rows, list):
                raise ValueError("SAP duplicate check did not return an OData results array")
            if rows or result.get("__next"):
                raise ValueError(
                    "SAP document reference already exists; reconcile instead of reposting"
                )
        payload = self.to_xml(record)
        response = self.send(
            "POST", self.endpoint, data=payload, headers={"Content-Type": "text/xml;charset=UTF-8"}
        )
        root = ET.fromstring(response.content)
        if root.find(f".//{{{SOAP}}}Fault") is not None:
            raise ValueError("SAP returned a SOAP fault; inspect the SAP service error log")
        confirmation = root.find(f".//{{{SFIN}}}JournalEntryBulkCreateConfirmation")
        if confirmation is None:
            raise ValueError("SAP response is missing JournalEntryBulkCreateConfirmation")
        severity = [node.text for node in confirmation.iter("MaximumLogItemSeverityCode")]
        severity.extend(node.text for node in confirmation.iter("SeverityCode"))
        notes = [node.text or "" for node in confirmation.iter("Note")]
        if not severity or any(code not in ("0", "1", "2") for code in severity):
            raise ValueError("SAP rejected journal entry: " + "; ".join(notes))
        entry = confirmation.find("JournalEntryCreateConfirmation/JournalEntryCreateConfirmation")
        if entry is None:
            raise ValueError("SAP did not return an individual journal-entry confirmation")
        company = entry.findtext("CompanyCode")
        year = entry.findtext("FiscalYear")
        document = entry.findtext("AccountingDocument")
        if company != record["company_code"]:
            raise ValueError("SAP confirmation company code does not match the request")
        details = {"simulated": record["test_data_indicator"], "notes": notes}
        if record["test_data_indicator"]:
            if not severity or not notes or (document and document.strip("0")):
                raise ValueError(
                    "Unexpected simulation result; reconcile SAP response before continuing"
                )
            return None, True, {"simulated": True, "customData": details}
        if not year or not document or not document.strip("0"):
            raise ValueError("SAP returned no posted document number: " + "; ".join(notes))
        details.update(company_code=company, fiscal_year=year, accounting_document=document)
        return f"{company}:{year}:{document}", True, {"customData": details}
