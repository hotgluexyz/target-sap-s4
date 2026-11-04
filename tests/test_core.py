"""Exercise the public Singer CLI and its accounting results."""

from __future__ import annotations

import copy
import json
from xml.etree import ElementTree as ET

import pytest
import requests
from click.testing import CliRunner

from target_sap_s4.sinks import JOURNAL_SCHEMA
from target_sap_s4.target import TargetSapS4

RECORD = {
    "externalId": "1710:walmart:check-001:USD",
    "company_code": "1710",
    "document_date": "2026-10-02",
    "posting_date": "2026-11-02",
    "header_text": "SPS & recovery",
    "items": [
        {
            "gl_account": "0010010000",
            "amount": "108.01",
            "currency": "USD",
            "debit_credit_code": "S",
        },
        {
            "gl_account": "0012550000",
            "amount": "-100.00",
            "currency": "USD",
            "debit_credit_code": "H",
            "item_text": "Inv <732982> & PO 123",
            "assignment_reference": "732982",
        },
        {
            "gl_account": "0012550000",
            "amount": "-8.01",
            "currency": "USD",
            "debit_credit_code": "H",
        },
    ],
}


def confirmation(document="0000000000", severity="1", note="Document check - no errors"):
    """Return the shape used by the synchronous SAP service."""
    return f"""<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/">
    <soap:Body><s:JournalEntryBulkCreateConfirmation xmlns:s="http://sap.com/xi/SAPSCORE/SFIN">
    <JournalEntryCreateConfirmation><JournalEntryCreateConfirmation>
    <AccountingDocument>{document}</AccountingDocument><CompanyCode>1710</CompanyCode>
    <FiscalYear>2026</FiscalYear></JournalEntryCreateConfirmation><Log>
    <MaximumLogItemSeverityCode>{severity}</MaximumLogItemSeverityCode>
    <Item><SeverityCode>{severity}</SeverityCode><Note>{note}</Note></Item>
    </Log></JournalEntryCreateConfirmation><Log/></s:JournalEntryBulkCreateConfirmation>
    </soap:Body></soap:Envelope>""".encode()


def run_cli(tmp_path, monkeypatch, records, response=None, config=None, prior=None, remote=None):
    """Feed Singer messages to the CLI while replacing only the HTTP transport."""
    monkeypatch.chdir(tmp_path)
    (tmp_path.parent / ".secrets").mkdir(exist_ok=True)
    monkeypatch.setattr(TargetSapS4, "incremental_target_state_path", str(tmp_path / "unused.json"))
    settings = {
        "base_url": "https://sap.example.com",
        "username": "demo",
        "password": "not-a-secret",
    }
    settings.update(config or {})
    path = tmp_path / "config.json"
    path.write_text(json.dumps(settings))
    requests_sent = []

    def transport(session, method, url, **kwargs):
        requests_sent.append((method, url, kwargs))
        result = requests.Response()
        result.status_code = 200
        if method == "GET":
            result._content = json.dumps({"d": {"results": remote or []}}).encode()
        elif isinstance(response, Exception):
            raise response
        else:
            result._content = response or confirmation()
        return result

    monkeypatch.setattr(requests.Session, "request", transport)
    # A source's Singer schema need not know SAP's account length constraint.
    input_schema = copy.deepcopy(JOURNAL_SCHEMA)
    input_schema["properties"]["items"]["items"]["properties"]["gl_account"].pop("maxLength")
    messages = [
        {
            "type": "SCHEMA",
            "stream": "journal_entries",
            "schema": input_schema,
            "key_properties": ["externalId"],
        }
    ]
    messages.extend({"type": "RECORD", "stream": "journal_entries", "record": r} for r in records)
    args = ["--config", str(path)]
    if prior:
        state_path = tmp_path / "prior.json"
        state_path.write_text(json.dumps(prior))
        args += ["--state", str(state_path)]
    result = CliRunner().invoke(
        TargetSapS4.cli, args, input="\n".join(map(json.dumps, messages)) + "\n"
    )
    states = [json.loads(line) for line in result.stdout.splitlines() if line.startswith("{")]
    assert states, (result.output, result.exception)
    return states[-1], requests_sent


def test_simulation_signed_amounts_xml_references_and_no_remote_id(tmp_path, monkeypatch):
    state, sent = run_cli(tmp_path, monkeypatch, [RECORD])
    bookmark = state["bookmarks"]["journal_entries"][0]
    assert bookmark["success"] is True
    assert bookmark["externalId"] == RECORD["externalId"]
    assert bookmark["simulated"] is True
    assert "id" not in bookmark
    root = ET.fromstring(sent[0][2]["data"])
    assert [n.text for n in root.iter("TestDataIndicator")] == ["true", "true"]
    assert [n.text for n in root.iter("AmountInTransactionCurrency")] == [
        "108.01",
        "-100.00",
        "-8.01",
    ]
    assert root.findtext(".//GLAccount") == "0010010000"
    assert root.findtext(".//DocumentItemText") == "Inv <732982> & PO 123"
    assert root.findtext(".//AssignmentReference") == "732982"
    assert sent[0][2]["allow_redirects"] is False
    assert sent[0][2]["verify"] is True


def test_profitability_assignment_is_preserved_on_its_own_line(tmp_path, monkeypatch):
    record = copy.deepcopy(RECORD)
    record["items"][1].update(account_assignment_type="EO", profit_center="TESTPC01")
    record["items"][2]["cost_center"] = "TESTCC02"
    state, sent = run_cli(tmp_path, monkeypatch, [record])
    assert state["bookmarks"]["journal_entries"][0]["success"] is True
    items = ET.fromstring(sent[0][2]["data"]).findall(".//JournalEntry/Item")
    assert items[0].find("AccountAssignment") is None
    assert [(e.tag, e.text) for e in items[1].find("AccountAssignment")] == [
        ("AccountAssignmentType", "EO"),
        ("ProfitCenter", "TESTPC01"),
    ]
    assert [(e.tag, e.text) for e in items[2].find("AccountAssignment")] == [
        ("CostCenter", "TESTCC02"),
    ]


@pytest.mark.parametrize(
    "defect", ["unbalanced", "wrong_sign", "long_account", "mixed_currency", "invalid_date"]
)
def test_invalid_journal_is_a_failed_bookmark_without_http(tmp_path, monkeypatch, defect):
    record = copy.deepcopy(RECORD)
    if defect == "unbalanced":
        record["items"][0]["amount"] = "108.02"
    elif defect == "wrong_sign":
        record["items"][1]["debit_credit_code"] = "S"
    elif defect == "long_account":
        record["items"][1]["gl_account"] = "DEDUCTION_GL"
    elif defect == "mixed_currency":
        record["items"][1]["currency"] = "EUR"
    else:
        record["posting_date"] = "2026-02-30"
    state, sent = run_cli(tmp_path, monkeypatch, [record])
    assert state["summary"]["journal_entries"]["fail"] == 1
    assert not sent


@pytest.mark.parametrize(
    "response",
    [
        confirmation(severity="3", note="Posting period closed"),
        b"<html/>",
        b'<s:Envelope xmlns:s="http://schemas.xmlsoap.org/soap/envelope/"><s:Body><s:Fault/></s:Body></s:Envelope>',
    ],
)
def test_http_200_is_not_business_success(tmp_path, monkeypatch, response):
    state, _ = run_cli(tmp_path, monkeypatch, [RECORD], response=response)
    assert state["summary"]["journal_entries"]["fail"] == 1
    assert not state["bookmarks"]["journal_entries"][0]["success"]


def test_post_returns_composite_id_and_existing_remote_reference_blocks_retry(
    tmp_path, monkeypatch
):
    state, sent = run_cli(
        tmp_path,
        monkeypatch,
        [RECORD],
        response=confirmation("1900000123"),
        config={"test_data_indicator": False, "sap_client": "100"},
    )
    assert state["bookmarks"]["journal_entries"][0]["id"] == "1710:2026:1900000123"
    assert [r[0] for r in sent] == ["GET", "POST"]
    assert sent[0][2]["params"]["sap-client"] == "100"
    state, sent = run_cli(
        tmp_path,
        monkeypatch,
        [RECORD],
        config={"test_data_indicator": False},
        remote=[{"AccountingDocument": "1900000123"}],
    )
    assert [r[0] for r in sent] == ["GET"]
    assert state["summary"]["journal_entries"]["fail"] == 1


def test_repeat_simulation_skips_unchanged_but_does_not_suppress_a_real_post(tmp_path, monkeypatch):
    state, _ = run_cli(tmp_path, monkeypatch, [RECORD])
    repeated, sent = run_cli(tmp_path, monkeypatch, [RECORD], prior=state)
    assert not sent
    assert repeated["summary"]["journal_entries"]["existing"] == 1
    posted, sent = run_cli(
        tmp_path,
        monkeypatch,
        [RECORD],
        prior=state,
        config={"test_data_indicator": False},
        response=confirmation("1900000124"),
    )
    assert [r[0] for r in sent] == ["GET", "POST"]
    assert posted["bookmarks"]["journal_entries"][-1]["id"] == "1710:2026:1900000124"


def test_ambiguous_posting_timeout_is_not_retried(tmp_path, monkeypatch):
    state, sent = run_cli(
        tmp_path,
        monkeypatch,
        [RECORD],
        response=requests.ReadTimeout(),
        config={"test_data_indicator": False},
    )
    assert [r[0] for r in sent] == ["GET", "POST"]
    assert state["summary"]["journal_entries"]["fail"] == 1
    assert "Reconcile before retrying" in state["bookmarks"]["journal_entries"][0]["error"]


@pytest.mark.parametrize("simulate,document", [(False, "0000000000"), (True, "1900000123")])
def test_response_must_match_simulation_or_posting_mode(tmp_path, monkeypatch, simulate, document):
    state, _ = run_cli(
        tmp_path,
        monkeypatch,
        [RECORD],
        response=confirmation(document),
        config={"test_data_indicator": simulate},
    )
    assert state["summary"]["journal_entries"]["fail"] == 1
    assert "id" not in state["bookmarks"]["journal_entries"][0]
