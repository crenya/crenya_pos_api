### Crenya POS API

Frappe/ERPNext backend for the Crenya offline-first desktop POS till (Oman retail).

The till keeps selling while offline and later pushes its sales and sales
returns to ERPNext through an idempotent sync protocol. Everything the till
needs (items, prices, customers, stock, taxes, payment modes) comes from an
ERPNext **POS Profile**. Invoices are created as normal POS Sales Invoices
through the ERPNext controllers, so stock, accounting, permissions and Fawtara
e-invoicing (`oman_compliance`, hooked on `Sales Invoice.on_submit`) behave
exactly as for invoices entered in the desk.

Supported: ERPNext / Frappe v15 (written to stay compatible with v16).

#### What the app adds

- DocType **Crenya POS Device**: one row per till (`device_id` UUID, stable
  short code `D01`, `D02`, … used in offline numbers `OFF-D02-000123`).
- DocType **Crenya Sync Event**: idempotency log of every pushed event
  (read-only in desk). Successful events older than
  `crenya_pos_event_retention_days` (default 120, never less than 90) are
  purged by a daily job; failed events are kept.
- Role **Crenya POS User**.
- Custom fields (created on install and on every migrate):
  - Sales Invoice: `crenya_local_id` (unique), `crenya_offline_number`, `crenya_device`
  - Customer: `crenya_local_id` (unique)
  - Item: `crenya_item_name_ar` (Item Name (Arabic))
  - Company: `crenya_company_name_ar`, `crenya_cr_number` (CR Number)

#### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench --site <site> install-app crenya_pos_api   # requires erpnext
bench --site <site> migrate
```

Optional `site_config.json` keys:

| key | default | meaning |
|---|---|---|
| `crenya_pos_total_tolerance` | `0.010` | max difference between till and server grand total, and between payments and payable total |
| `crenya_pos_event_retention_days` | `120` | days to keep successful sync events (minimum 90) |
| `crenya_pos_pull_lag_seconds` | `5` | records modified in the last N seconds are delivered on the next pull, so late-committing transactions are never skipped |

#### POS Profile setup checklist (Oman)

1. **Currency precision**: System Settings → Currency Precision = `3`
   (ERPNext rounds all currency fields with the system precision). Currency
   `OMR` enabled; company default currency `OMR`.
2. **Rounding**: tick *Disable Rounded Total* on the POS Profile
   (recommended), or set Currency OMR → *Smallest Currency Fraction Value* =
   `0.005` if you want cash rounding to 5 baisa.
3. **VAT**: an Account `VAT 5%` (type Tax) and a *Sales Taxes and Charges
   Template* `Oman VAT 5%` with a single row: Charge Type *On Net Total*,
   Rate `5`, **Is this Tax included in Basic Rate?** ticked. Only *On Net
   Total* rows are supported offline; other charge types are rejected with
   `unsupported_tax`. Set it as the POS Profile's *Taxes and Charges*.
4. **Zero rated / exempt items**: an *Item Tax Template* `Zero Rated`
   (VAT account at rate 0) for the company, added to the Item's *Taxes* table.
5. **Customer**: a `Walk-in Customer` set as the POS Profile default customer.
   Selling Settings default Customer Group / Territory are used for customers
   created at the till.
6. **Payment modes**: `Cash` (type Cash) and `Card` (type Bank), each with a
   default account for the company, added to the POS Profile payments (one
   marked default — rounding differences within tolerance go to it).
7. Warehouse, Cost Center, Selling Price List (in OMR), Write Off Account /
   Cost Center, optional Item Groups / Customer Groups (sub-groups are
   included automatically).
8. **Users**: create an API key/secret per cashier (or per till integration
   user). Roles: **Crenya POS User**, **Sales User** (create customers) and
   **Accounts User** (create and submit Sales Invoices — ERPNext permissions
   are enforced, nothing is inserted with `ignore_permissions`). Add the user
   to the POS Profile's *Applicable for Users* table, or leave that table empty
   to allow everyone.

#### API summary

All methods are `POST {server}/api/method/crenya_pos_api.api.<module>.<fn>` with
`Authorization: token <api_key>:<api_secret>`. Money and quantities are decimal
strings. The full contract (payloads, error codes, idempotency rules) is the
sync protocol document of the till: `apps/pos-desktop/docs/sync-protocol.md`.

| method | purpose |
|---|---|
| `sync.get_sync_capabilities` | ping, versions, features (GET or POST) |
| `device.list_pos_profiles` | POS Profiles the user may use |
| `device.register_device` | idempotent device registration, assigns `D01`… |
| `device.get_bootstrap` | profile, company, taxes, payment modes for the till |
| `sync.pull_changes` | keyset-paginated feed: `item`, `item_price`, `customer`, `stock` + tombstones |
| `sync.push_batch` | up to 50 events (Customer / Sales Invoice submit), one savepoint + commit per event |
| `returns.get_invoice_for_return` | original invoice (by name or offline number) with returned / returnable qty |

Request-level errors carry `error: {code, message, retryable}` in the JSON
body next to Frappe's `exc_type`; per-event errors are returned inside the
`push_batch` results.

#### Tests

Pure unit tests (no site needed):

```bash
cd ~/frappe-bench
PYTHONPATH=apps/crenya_pos_api env/bin/python -m unittest discover \
  -s apps/crenya_pos_api/crenya_pos_api/tests -p 'test_unit_*.py'
```

Integration tests (need a disposable site with ERPNext and this app installed;
they create `_Test Crenya …` masters with OMR and 5 % inclusive VAT and commit
them):

```bash
bench --site <test-site> set-config allow_tests true
bench --site <test-site> run-tests --app crenya_pos_api
```

#### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/crenya_pos_api
pre-commit install
```

#### License

mit
