# target-sap-s4

New Hotglue Singer target for SAP S/4HANA's **standard** synchronous journal-entry
SOAP API (`JournalEntryCreateRequestConfirmation_In`). Generated from the Hotglue
target cookiecutter, independently of `target-sap-onprem`. No custom `Z*` services.

## Configuration

Use Python 3.10 and a dedicated environment:

```bash
uv venv --python 3.10 --seed .venv
uv pip install --python .venv/bin/python -e . pytest ruff
.venv/bin/target-sap-s4 --config /secure/config.json < /secure/data.singer
```

Example shape (not usable credentials):

```json
{
  "base_url": "https://sap.example.com:44301",
  "username": "INTEGRATION_USER",
  "password": "REPLACE_VIA_SECRET_STORAGE",
  "sap_client": "100",
  "verify_ssl": true,
  "test_data_indicator": true,
  "timeout": 60
}
```

`base_url`, `username`, and `password` are required; `sap_client` is optional.
The URL must be an HTTPS origin, without a path, query, or embedded credentials.
TLS verification and simulation default to **true**. Disable TLS verification
only for an explicitly trusted test instance with a self-signed certificate.
Set `test_data_indicator: false` only with permission to post accounting data.

## Stream: journal_entries

The input Singer schema must describe the record fields below. The target also
enforces its own SAP-specific contract; see `JOURNAL_SCHEMA` in `sinks.py`.

```json
{
  "externalId": "company:retailer:check:currency",
  "company_code": "1710",
  "document_date": "2026-10-02",
  "posting_date": "2026-11-02",
  "document_type": "SA",
  "header_text": "Recovery check 123",
  "items": [
    {"gl_account": "10010000", "amount": "108.00", "currency": "USD", "debit_credit_code": "S"},
    {"gl_account": "12550000", "amount": "-108.00", "currency": "USD", "debit_credit_code": "H",
     "item_text": "Invoice 732982 PO 123", "assignment_reference": "732982"}
  ]
}
```

These example accounts are not business mapping defaults. Accounts and company
are tenant decisions. Amounts are signed decimal **strings**, debit `S` positive
and credit `H` negative. Items must balance exactly in one transaction currency.
Dates must be real ISO dates. Optional item fields: `reference_document_item`,
`cost_center`, `profit_center`, and `account_assignment_type` (only `EO`, for a
profitability segment). `EO` is serialized as `AccountAssignmentType` before
`ProfitCenter` and `CostCenter`. Supply tenant-approved assignments: cost centers
alone may not satisfy revenue/sales-deduction accounts. Simulation validates the
specific account, assignment and posting date; an existing profit center does not
establish the correct business allocation. Long account/text/reference fields fail, never
truncate. This initial target handles normal G/L journals, not customer payments,
AR clearing, tax calculation, negative posting, updates, or formal reversals.

## Results and duplicate safety

- Both SOAP message headers explicitly carry `TestDataIndicator`.
- SAP business errors and SOAP faults fail even with HTTP 200.
- Successful simulations have `simulated: true` and **no remote ID**. A successful
  simulation bookmark means validation succeeded, not that a document was posted.
- Posted IDs are `CompanyCode:FiscalYear:AccountingDocument`. `externalId` remains
  the source identity; SAP identifiers and response notes are retained in state.
- The SAP reference is a deterministic 16-character hash of company + external ID.
  The human check/invoice/PO references belong in header/item fields, not that hash.
- SDK successful-record hashing skips unchanged records with prior state. The hash
  includes simulation mode and SAP host/client, so validation cannot suppress a
  later real posting or a run against a different destination. Pass saved target
  state with `--state`; preserve normal Hotglue snapshots/bookmarks in jobs.
- Before a real POST, query the operational journal-item API for the scoped SAP
  reference. If it already exists without an exact prior-state match, **fail for
  reconciliation**, rather than silently creating, updating, or assuming success.
  Real posting therefore requires read access to `API_OPLACCTGDOCITEMCUBE_SRV` too.
- No automatic write retries, including on timeouts, 429, or 5xx. A failed request
  may have committed remotely. Reconcile before retrying. There is no claim of
  server-enforced exactly-once semantics: serialize jobs per tenant/company and
  do not run concurrent imports of the same check.
- Financial documents with a resolved/explicit existing ID are never overwritten.
  Corrections or additional recovery lines require an explicit accounting policy.

## Checks

```bash
.venv/bin/pytest -q
.venv/bin/ruff check .
.venv/bin/ruff format --check .
```

Tests use the public Singer CLI and a replaced HTTP transport. Actual creation
still requires an authorized sandbox posting test; simulation is not proof of
commit, number-range allocation, or duplicate behavior in a real SAP posting.
