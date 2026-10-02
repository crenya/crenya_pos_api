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
- DocType **Crenya POS Shift** (named after the till's shift `local_id`) with
  child table **Crenya POS Shift Payment** (mode of payment, expected,
  counted, difference): one row per cashier shift closed at a till, with
  opening float, sales / returns counts and totals, net and tax totals, notes,
  and `invoice_count_on_server` (Sales Invoices carrying the shift's ID when
  the shift was pushed). `opened_at` / `closed_at` arrive as UTC and are stored
  in the site time zone. Read-only in desk (Accounts Manager / Accounts User /
  System Manager can read; only System Manager can delete).
- DocType **Crenya POS Release** (named after its `version`) with child table
  **Crenya POS Release Artifact** (target, private file, signature): till
  builds offered to the tills' updater (see *Till updates*). System Manager
  manages releases; Crenya POS User and Accounts User can read them.
  **Crenya POS Device** has an *Update Channel* (`stable` / `beta`).
- DocType **Crenya Cash Denomination** (currency, value, label, kind
  `note` / `coin`, enabled): the notes and coins tills offer when cashiers
  count cash (see *Locale data*). System Manager manages them; Crenya POS
  User can read them. The app creates none.
- Role **Crenya POS User**.
- Custom fields (created on install and on every migrate):
  - Sales Invoice: `crenya_local_id` (unique), `crenya_offline_number`, `crenya_device`,
    `crenya_shift_id` (shift of the till that sold it, in standard filter),
    `crenya_cashier` (POS Cashier: Link User, the till cashier who rang up the sale, in standard filter)
  - User: section **Crenya POS** on the *Roles & Permissions* tab with `crenya_pos_pin`
    (POS PIN, Password) and the hidden `crenya_pos_pin_hash` (see *Cashier PINs*)
  - Customer: `crenya_local_id` (unique)
  - Item: `crenya_item_name_ar` (Item Name (Arabic))
  - Company: `crenya_company_name_ar`, `crenya_cr_number` (CR Number), and a collapsible
    section **Crenya POS** (after the address) with the receipt wording `crenya_tax_name`,
    `crenya_tax_id_label`, `crenya_invoice_title`, `crenya_credit_note_title` (each with an
    `_ar` Arabic twin) and `crenya_receipt_qr` (Receipt QR Code: Verification link / ZATCA (KSA) /
    None); see *Tax and invoice wording*
  - POS Profile: `crenya_allow_return_without_invoice` (Allow returns without invoice
    (Crenya POS), Check, default off; see *Returns without an invoice*)

#### Installation

```bash
cd $PATH_TO_YOUR_BENCH
bench get-app $URL_OF_THIS_REPO --branch main
bench --site <site> install-app crenya_pos_api   # requires erpnext
bench --site <site> migrate
```

The role and custom fields are created after install and after every migrate.
If a deploy did not run the app's `after_migrate` hook (custom fields missing,
e.g. no *Crenya POS* section on Company), a System Manager can apply the same
idempotent setup without a migrate:

```bash
curl -X POST "$SITE/api/method/crenya_pos_api.api.setup.ensure_custom_fields" \
  -H "Authorization: token <api_key>:<api_secret>"
```

It returns `{role, custom_fields: {doctype: [fieldnames]}}`; anyone else gets
HTTP 403. Running it again changes nothing.

Optional `site_config.json` keys:

| key | default | meaning |
|---|---|---|
| `crenya_pos_total_tolerance` | 10 × the currency's smallest unit (`0.10` with 2 decimals, `0.010` with 3) | max difference between till and server grand total, and between payments and payable total |
| `crenya_pos_event_retention_days` | `120` | days to keep successful sync events (minimum 90) |
| `crenya_pos_pull_lag_seconds` | `5` | records modified in the last N seconds are delivered on the next pull, so late-committing transactions are never skipped |
| `crenya_pos_verify_rate_limit` | `60` | requests per minute and IP address to the public receipt verification page |

#### POS Profile setup checklist (example: Oman)

The steps use an Omani shop as the example; nothing in the app is specific to
it. Use your own currency, precision, taxes and time zone.

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
   `unsupported_tax`. Set it as the POS Profile's *Taxes and Charges*. Further
   templates of the company can be offered for switching at the till (see
   *Tax templates*).
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
8. **Users**: cashiers sign in on the till with their normal ERPNext
   username + password (2FA must be off for till users). On first sign-in the
   app creates the user's API key pair and hands it to the till; regenerating
   the keys in ERPNext signs every till of that user out. Roles: **Crenya POS User**, **Sales User** (create customers) and
   **Accounts User** (create and submit Sales Invoices — ERPNext permissions
   are enforced; only the app's own records, Crenya POS Device, Crenya Sync
   Event and Crenya POS Shift, are written with `ignore_permissions` after the
   user / device has been authorized). Add the user
   to the POS Profile's *Applicable for Users* table, or leave that table empty
   to allow everyone.

#### API summary

All methods are `POST {server}/api/method/crenya_pos_api.api.<module>.<fn>` with
`Authorization: token <api_key>:<api_secret>`, except `auth.login`, which the
till calls without auth to exchange username + password for that key pair, and
the `update.*` methods, which are `GET` (same token auth). Money and quantities are decimal
strings. The full contract (payloads, error codes, idempotency rules) is the
sync protocol document of the till: `apps/pos-desktop/docs/sync-protocol.md`.

| method | purpose |
|---|---|
| `auth.login` | username + password → user's API key pair (guest, POST, rate limited 10 / 5 min, desk lockout rules apply) |
| `sync.get_sync_capabilities` | ping, versions, features incl. `shifts`, `tax_templates`, `loyalty`, `promotions`, `verify_page`, `batches`, `open_returns`, `tax_wording` (GET or POST) |
| `device.list_pos_profiles` | POS Profiles the user may use |
| `device.register_device` | idempotent device registration, assigns `D01`… |
| `device.get_bootstrap` | profile (incl. `allow_negative_stock` from Stock Settings, `tax_templates`, `loyalty_enabled`, `allow_return_without_invoice`), company (incl. `phone_country_code` and the tax / invoice wording), `settings.qty_precision`, taxes, payment modes, and the locale data `currency`, `phone_country_codes`, `cash_denominations`, `site_timezone` for the till |
| `sync.pull_changes` | keyset-paginated feed: `item`, `item_price`, `customer`, `stock`, `cashier`, `item_group`, `pricing_rule`, `batch` + tombstones |
| `sync.push_batch` | up to 50 events (Customer / Sales Invoice / Crenya POS Shift submit), one savepoint + commit per event |
| `loyalty.get_details` | redeemable loyalty points of a Customer (`device_id`, `customer`; POST) |
| `fawtara.get_status` | Fawtara status and ASP document id of up to 50 till invoices (`device_id`, `local_ids`; POST) |
| `returns.get_invoice_for_return` | original invoice (by name or offline number) with returned / returnable qty, `batch_no` / `expiry_date` per row, `taxes_and_charges` and `loyalty_amount` |
| `setup.ensure_custom_fields` | create / update the app's role and custom fields, as after a migrate (System Manager only, POST, idempotent) |
| `cashier.clear_pin` | remove a cashier's POS PIN (`user`; System Manager only, POST) |
| `update.check` | newest published release for the till (`device_id`, `target`, `current_version`); raw updater JSON or HTTP 204 (GET) |
| `update.download` | the release's installer for `target` (`release`, `target`, `device_id`; GET) |

#### Shifts

When a cashier closes a shift the till queues one `push_batch` event with
`aggregate_type = "Crenya POS Shift"` and `operation = "submit"`. The server
creates the **Crenya POS Shift** (with its payments table) and records how
many Sales Invoices with `crenya_shift_id = local_id` it already has. The
event is idempotent on `event_id` and on the shift `local_id`, like Customer
events, and depends on nothing: invoices of the shift may be pushed before or
after it. Each Sales Invoice payload may carry `shift_local_id` (string or
null, optional for older tills), stored in `crenya_shift_id` so ERPNext can
report per shift. The per-event result is the standard one with
`doctype: "Crenya POS Shift"`, `docstatus: 0` and `totals: null`.

Request-level errors carry `error: {code, message, retryable}` in the JSON
body next to Frappe's `exc_type`; per-event errors are returned inside the
`push_batch` results.

#### Tax templates

The till can switch an invoice to another *Sales Taxes and Charges Template*
of the company, for example a zero-rated or a VAT-exclusive one. Bootstrap
`profile.tax_templates` lists every enabled template of the POS Profile's
company whose rows are all *On Net Total* (the till computes them offline),
each with `name`, `title`, `is_default` and its `taxes` rows (`account_head`,
`description`, `rate`, `included_in_print_rate`). The POS Profile's own
template is always listed first with `is_default: true`; templates with any
other charge type (*Actual*, *On Previous Row Total*, …) are left out, and
only the profile's own template makes bootstrap fail with `unsupported_tax`.

A Sales Invoice payload's `taxes_and_charges` is applied when it is one of
those templates; any other value (disabled, another company, unsupported
rows, unknown) fails the event with `validation`. Omitted or null means the
POS Profile's template. To offer a template, create it for the company, keep
all rows *On Net Total* and leave it enabled; tills pick it up on their next
bootstrap. Bill discounts need nothing extra: they arrive as line rates.

#### Tax and invoice wording

The till hardcodes no tax name, tax ID label, invoice title or QR format, so
the same till prints Omani, Emirati, Saudi or Indian receipts. Bootstrap's
`company` block carries, per company:

| key | from Company | when blank |
|---|---|---|
| `country` | *Country* | `null` |
| `tax_name`, `tax_name_ar` | *Tax Name* (`crenya_tax_name`, `_ar`) | the leading words of the POS Profile template's first tax row description (else its account, without the company abbreviation): "VAT 5%" → `VAT`, "CGST @ 9" → `CGST`; `_ar` `null` |
| `tax_id_label`, `tax_id_label_ar` | *Tax ID Label* (`crenya_tax_id_label`, `_ar`) | the label of Company *Tax ID* as the site shows it ("Tax ID"), or of India Compliance's `gstin` field when the site has it; `_ar` `null` |
| `invoice_title`, `invoice_title_ar` | *Invoice Title* (`crenya_invoice_title`, `_ar`) | `Tax Invoice`; `_ar` `null` |
| `credit_note_title`, `credit_note_title_ar` | *Credit Note Title* (`crenya_credit_note_title`, `_ar`) | `Credit Note`; `_ar` `null` |
| `receipt_qr` | *Receipt QR Code* (`crenya_receipt_qr`): Verification link → `verify_link`, ZATCA (KSA) → `zatca_tlv`, None → `none` | `verify_link` |
| `tax_code_label` | label of Item `gst_hsn_code` (India Compliance's HSN/SAC) | `null` when the site has no such field |
| `tax_id` | *Tax ID*, else `gstin` when the site has that field | `null` |

- **Item records** carry `tax_code`: Item `gst_hsn_code` when the site has
  that field, else `null`.
- **Customer records**: `tax_id` falls back to the customer's `gstin` when the
  site has that field and *Tax ID* is empty.
- With *Receipt QR Code* = ZATCA (KSA) the till prints the ZATCA phase 1 QR
  (seller name, VAT number = `tax_id`, time stamp, total, VAT) offline. ZATCA
  phase 2 and India e-invoicing (IRN) are done by the country's compliance app
  in ERPNext after sync.

Examples: Saudi Arabia: Tax ID Label `VAT No.`, Invoice Title `Simplified Tax
Invoice` / `فاتورة ضريبية مبسطة`, Receipt QR Code *ZATCA (KSA)*. UAE: Tax ID
Label `TRN`. India: install India Compliance (GSTIN, HSN/SAC come from its
fields; a CGST + SGST template prints both rows). Oman: nothing to set
(`VAT`, `Tax ID`, `Tax Invoice`, verification link), or Tax ID Label `VATIN`.

`sync.get_sync_capabilities` announces this with `features.tax_wording`.

#### Locale data

The app hardcodes no currency, symbol, decimals, number format, dialling
code, country, tax label, denomination or time zone; bootstrap sends what the
till needs, read from the site:

- `currency`: `{code, symbol, symbol_on_right, fraction, fraction_units,
  number_format, precision, smallest_currency_fraction_value}` from the POS
  Profile currency's **Currency** record. `number_format` falls back to
  System Settings; `precision` is the money precision ERPNext rounds invoice
  totals with (the same as `profile.currency_precision`). Empty values are
  `null`. To change what the till shows, edit the Currency record.
- `phone_country_codes`: `[{iso, name, code}]` from Frappe's country data
  (`frappe.geo.country_info`, field `isd`): the company's country first, then
  every other country by name; countries without a dialling code are left
  out. `company.phone_country_code` is the dialling code of the Company's
  *Country*, or `""` when the company has none (no fallback country).
- `cash_denominations`: `[{value, label, kind}]`, highest value first, from
  the enabled **Crenya Cash Denomination** records of the profile currency;
  `[]` when none are configured (the till then asks only for the cash total).
- `site_timezone`: System Settings *Time Zone* (also in
  `sync.get_sync_capabilities`); tills use it for posting dates / times and
  "today", not the PC's zone.
- Totals tolerance: `site_config.crenya_pos_total_tolerance` when set, else
  10 × the smallest unit of the invoice currency's precision (`0.10` with 2
  decimals, `0.010` with 3).

To add denominations: *Crenya Cash Denomination* → New, pick the *Currency*,
enter the *Value* (face value, e.g. `20` or `0.5`), a *Label* as printed on
the note or coin (shown on the till), *Kind* `note` or `coin`, and keep
*Enabled* ticked. A currency may have each value once. Untick *Enabled* to
hide a denomination; tills pick up changes on their next bootstrap.

#### Loyalty points

Uses ERPNext's own **Loyalty Program**. Setup: create a Loyalty Program for
the company (collection rules, *Conversion Factor* = currency value of one
point, *Expense Account* and *Cost Center* for redemptions, *From Date* not in
the future) and set it as the Customer's *Loyalty Program* (or tick *Auto Opt
In* so new customers are enrolled). Bootstrap `profile.loyalty_enabled` is
true when an active program exists for the company.

- **Earning**: a till sale to an enrolled customer carries the customer's
  program, so ERPNext books the earned points on submit exactly as for a desk
  invoice. A till return against an original invoice carries the original's
  program, so ERPNext re-books the points of the original sale (less the
  returned amount) on submit, and again if the return is cancelled. As in the
  desk, a return fails with `validation` when points earned on the original
  have already been redeemed. Returns without a reference do not touch points.
- **Balance**: `loyalty.get_details(device_id, customer)` (POST, online only)
  returns `{customer, loyalty_program, loyalty_points, conversion_factor,
  max_redeemable_amount, currency}` for the Customer (ERP name): the current
  redeemable points from ERPNext's Loyalty Point Entries,
  `max_redeemable_amount` = points × conversion factor rounded down to the
  currency precision. `loyalty_program` is null (0 points) when the customer
  is not enrolled in a program of the till's company.
- **Redemption**: a Sales Invoice payload may carry
  `loyalty: {"points": 120, "amount": "1.200"}` (points a positive integer,
  amount a positive decimal string). The server sets `redeem_loyalty_points`,
  `loyalty_points`, `loyalty_program` (the customer's) and `loyalty_amount`;
  ERPNext checks the balance when the invoice is saved and posts the
  redemption to the program's expense account. The payments must cover
  `rounded_total − loyalty_amount` (ERPNext counts the loyalty amount as
  paid), within the usual tolerance; an invoice paid entirely with points
  is sent with a zero payment row of the default mode of payment, which
  ERPNext clears on submit. `validation` when: the invoice is a return, the
  customer is the POS Profile's default customer, the customer has no loyalty
  program (of the company), the program is inactive or has no expense
  account, `amount` is more than points × conversion factor or more than the
  tolerance below it, `amount` exceeds the invoice total, or the customer
  does not have enough points.
- **Returns of redeemed invoices**: `returns.get_invoice_for_return` returns
  the original's `taxes_and_charges` and `loyalty_amount` (`"0.000"` when no
  points were redeemed). An invoice partly paid with points must be returned
  from ERPNext: tills refuse it, and a till return against it (by
  `return_against` or `return_against_local_id`) fails with `validation`
  "Return this invoice from ERPNext: it was partly paid with loyalty points".

`sync.get_sync_capabilities` announces both with `features.tax_templates`
and `features.loyalty`.

#### Promotions (Pricing Rules)

Promotions are ERPNext's own **Pricing Rules** (and the rules a **Promotional
Scheme** generates). Tills pull them with `pull_changes` entity
`pricing_rule`, evaluate them offline and send the result as line rates, so
nothing changes in how invoices are checked: the server keeps
`ignore_pricing_rule = 1` and the till total must match within tolerance.

Sent as active (`disabled: 0`): enabled selling rules of the POS Profile's
company (or without company) that have not ended. A rule is sent with
`disabled: 1` (the till drops it) when it is disabled, its *Valid Upto* has
passed, or the till cannot evaluate it: buying-only, another company, a
*Condition*, *Coupon Code Based*, *Applicable For* other than empty /
Customer / Customer Group, *Apply Rule On Other*, a margin (margin type with a
non-zero rate), *Validate Applied Rule* (ERPNext never applies those by
itself), or a currency other than the till's. Every changed rule is sent, so a
rule that stops qualifying reaches the tills with `disabled: 1`; deleted rules
arrive as tombstones. A rule that simply runs out is not re-sent when its end
date passes (nothing changes on the rule); tills check the dates themselves.

Record: `name, title, disabled, priority, apply_on, items [{item_code, uom}],
item_groups, brands, mixed_conditions, is_cumulative, applicable_for,
customer, customer_group, min_qty, max_qty, min_amt, max_amt, valid_from,
valid_upto, price_or_product_discount, rate_or_discount, rate,
discount_percentage, discount_amount, apply_discount_on_rate,
apply_multiple_pricing_rules, for_price_list, warehouse, threshold_percentage,
same_item, free_item, free_qty, free_item_uom, free_item_rate, round_free_qty,
is_recursive, recurse_for, apply_recursion_over, promotional_scheme,
rule_description, modified`. Amounts are decimal strings with the currency
precision, quantities and percentages plain decimal strings; `0` means no
limit.

Item group rules cover sub-groups: item records carry `brand`, and entity
`item_group` sends the whole Item Group tree (`name, parent_item_group, lft,
rgt, modified`). ERPNext renumbers `lft` / `rgt` of other groups without
changing their `modified` when groups are added or moved, so tills build the
tree from `parent_item_group`.

Sales Invoice payload items may carry `pricing_rules` (list of up to 20 rule
names, each at most 140 characters, may be empty) and `is_free_item` (0/1);
tills without promotions omit both. The server stores `is_free_item` and the
names (as a JSON list in the row's *Pricing Rules*, only when the list is not
empty) on the Sales Invoice Item, for reporting; rule names are not checked
against existing rules, since a rule may be deleted before an offline sale
syncs. Free lines are ordinary lines at rate 0 (or the rule's free item
rate); ERPNext keeps them because it only adds or removes free lines itself
when pricing rules are applied, which `ignore_pricing_rule` turns off, and it
skips *Is Free Item* lines in the below-purchase-rate check.

The names are written to the rows in a `Sales Invoice` `before_submit` hook,
after ERPNext's last validation: when a saved invoice with
`ignore_pricing_rule` is validated again (as on submit), ERPNext clears the
rows' *Pricing Rules* and undoes the named rules (a *Discount Percentage* rule
resets the line rate to the price list rate), and a row with *Pricing Rules*
and a discount percentage is re-priced from its price list rate. Keeping the
names off the rows until then leaves the till's rates exactly as sent.

`sync.get_sync_capabilities` announces this with `features.promotions`.

#### Batches (pharmacy)

Batch tracked items (*Has Batch No*) are sold per batch, the ERPNext v15 way
(Serial and Batch Bundles); nothing is configured in the app itself.

- **Item records** carry `has_batch_no` and `has_expiry_date`.
- **Entity `batch`** of `pull_changes`: the batches of the profile's items
  (same item filter as `item`, batch tracked items only) as `name, item_code,
  expiry_date, manufacturing_date, disabled, qty, modified`. `qty` is the
  batch's stock in the POS Profile warehouse in stock UOM, counted as ERPNext
  counts batch stock (Serial and Batch Entries of the warehouse's non-cancelled
  Stock Ledger Entries, plus the `batch_no` of ledger entries from before
  bundles), with no reservations subtracted. Expired and disabled batches are
  sent like any other: the till decides by date what it may sell. A batch is
  sent again when the Batch changes **or** its stock in the warehouse moves:
  besides the Batch keyset the cursor keeps a second position (`sm` / `sn`) in
  the warehouse's Stock Ledger Entries of those items, ordered by
  (`modified`, `name`), so a sale at another till, a receipt or a cancellation
  re-sends the batches it touched with their current qty (one grouped query per
  page). Deleted batches arrive as tombstones.
- **Sales Invoice payload items** may carry `batch_no` (at most 140
  characters); tills split a line across batches themselves (FEFO), so a line
  has at most one batch. ERPNext rounds a row's `qty` to the precision of Sales
  Invoice Item *Qty* (a property setter on the field included, else System
  Settings → *Float Precision*) and derives `stock_qty` from it, so tills use a
  batch share only when it is exact at that precision; bootstrap sends it as
  `settings.qty_precision`. The server checks that the batch exists and belongs to
  the line's batch tracked item, then sets `batch_no` and
  `use_serial_batch_fields = 1` on the row; on submit ERPNext builds the row's
  Serial and Batch Bundle from those fields. ERPNext's own checks decide the
  rest: an expired batch (expiry before the posting date) or a batch without
  enough stock in the warehouse fail with `validation`. Lines without
  `batch_no` (tills without batch support) keep ERPNext's automatic pick, which
  needs Stock Settings → *Auto Create Serial and Batch Bundle For Outward*
  (on by default) and follows *Pick Serial / Batch Based On*.
- **Returns** take the batch from the original row, not from the till: a
  return row goes back into the original row's batch (its `batch_no`, or the
  single batch of its bundle). When the original row consumed several batches
  (for example a desk invoice that ERPNext auto-picked), the return line is
  split into one row per batch, each referencing the original row: shares are
  proportional to what each batch sold, capped by what it can still take back
  (sold less already returned, from ERPNext's
  `sales_and_purchase_return.get_available_serial_batches`), in whole units when
  the UOM must be a whole number. Returning more than the batches can take back
  fails with `validation`. Serial numbered items keep ERPNext's own return
  handling.
- `returns.get_invoice_for_return` items carry `batch_no` and `expiry_date`,
  both null when the row has no batch or consumed several.
- Batch prices (*Item Price* with a batch) are not used by tills yet.

`sync.get_sync_capabilities` announces this with `features.batches`.

#### Returns without an invoice

A till can refund items without the original invoice (the customer has no
receipt) when its POS Profile allows it: tick **Allow returns without invoice
(Crenya POS)** (`crenya_allow_return_without_invoice`, off by default). Bootstrap
sends it as `profile.allow_return_without_invoice`.

- **Payload**: a normal return (`is_return = 1`, negative quantities and refund
  payments) with no `return_against` / `return_against_local_id` and items
  without `against_row_name` / `against_line_no`. Rows carry `rate`, `uom`,
  `conversion_factor` and, for batch tracked items, `batch_no`, like a sale;
  `remarks` carries the return reason.
- **ERPNext document**: a POS Sales Invoice credit note without *Return
  Against*, `update_stock` from the profile (as for sales), `remarks` = the
  reason, negative payments. Batch rows get `batch_no` and
  `use_serial_batch_fields = 1`; on submit ERPNext builds an inward Serial and
  Batch Bundle into that batch. ERPNext checks batch expiry on outward rows
  only, so stock can go back into an **expired** batch (it stays unsellable:
  tills and ERPNext refuse expired batches on sales).
- **Valuation**: stock comes back at ERPNext's valuation, not at the selling
  rate. The server sets each stock row's *Incoming Rate* from ERPNext's
  `get_incoming_rate` (the batch's average rate in the warehouse for a batch row,
  or the warehouse valuation when the batch holds nothing; else the item's FIFO
  / LIFO / Moving Average rate in the warehouse; else the Item's *Valuation
  Rate*). ERPNext v15.120+ and v16 no longer compute it themselves for a
  standalone credit note and would otherwise book the stock at the row's selling
  rate. When no valuation exists at all, ERPNext's own rule applies. Selling
  Settings → *Set Incoming Rate as Zero for Expired Batch* is respected.
- **`validation` errors**: the profile does not allow returns without an
  invoice (checked when the return is pushed, not only when the till made it),
  `remarks` empty, a row referencing an original row, a quantity that is not
  negative or a refund that is positive, a batch tracked item row without
  `batch_no`, a batch of another item or a batch that does not exist, and
  ERPNext's own checks.
- Returns against an invoice are unchanged and do not need the flag.

`sync.get_sync_capabilities` announces this with `features.open_returns`.

#### Receipt verification (Fawtara)

Till receipts can carry a QR code that opens
`{site}/fawtara/verify?id=<crenya_local_id>`, a public page (no sign-in) in
English and Arabic. For a submitted Sales Invoice with that till id it shows
the seller's name (and Arabic name), tax number and CR number, the ERPNext
invoice number, the till's receipt number, posting date and time, invoice type
(tax invoice / credit note), grand total and tax in the invoice currency and,
when `oman_compliance` is installed, the Fawtara status and ASP document id.
It never shows customer data. An unknown id, or an invoice that has not synced
yet, gets a neutral "not found yet" page with HTTP 404. The page is never
cached and allows `crenya_pos_verify_rate_limit` (default 60) requests per
minute per IP address; more get HTTP 429.

`fawtara.get_status(device_id, local_ids)` (POST, at most 50 ids) returns
`[{local_id, name, fawtara_status, document_id}]` for the device company's
submitted invoices with those till ids (unknown ids are left out).
`fawtara_status` and `document_id` come from the invoice's *Fawtara Status* /
*ASP Document ID*, falling back to its Fawtara record, and are null when
`oman_compliance` is not installed or the invoice has not been reported yet.
Tills ask for their recent invoices whose status is not final.
`sync.get_sync_capabilities` announces the page with `features.verify_page`.

#### Cashier PINs

Several cashiers can work on one till, which stays signed in with its device
user's API keys. A cashier unlocks the till with a 4–6 digit **POS PIN** that
the till checks offline.

Setting a PIN: open the cashier's **User** in ERPNext, tab *Roles &
Permissions*, section **Crenya POS**, type the PIN into **POS PIN** and save.
The user needs the **Crenya POS User** role (and, if the POS Profile's
*Applicable for Users* table is filled, must be listed there). On save the
PIN must be 4 to 6 digits 0-9, otherwise the save is refused. The server
replaces it with a salted hash (`pbkdf2_sha256$120000$<salt>$<key>`,
PBKDF2-HMAC-SHA256, 16-byte random salt, 32-byte key, base64) in the hidden
field `crenya_pos_pin_hash` and empties the PIN field, so the PIN is stored
nowhere in readable or decryptable form. The PIN field therefore always looks
empty; leaving it empty keeps the current PIN, typing a new one replaces it.
Desk users can also set their own PIN under *My Settings*. To remove a PIN, a
System Manager calls `crenya_pos_api.api.cashier.clear_pin` with `user`
(e.g. `bench --site <site> execute crenya_pos_api.sync.cashier.clear_user_pin --args "['cashier1@store.om']"`).

Tills receive cashiers through `pull_changes` entity `cashier`:
`{name, full_name, enabled, pin_hash, modified}`, keyset-paginated on the
User's `modified`. It contains the holders of the **Crenya POS User** role
(limited to the profile's *Applicable for Users* when that table is not
empty) and users that have a PIN. `enabled` is 1 only for an enabled user who
still has the role and may use the profile; `pin_hash` is null when no PIN is
set and for every record with `enabled: 0`. Removing the role or disabling the
user saves the User, so the till receives `enabled: 0` on its next pull (a
user who loses the role and never had a PIN is simply no longer sent; without
a PIN they cannot unlock a till anyway); deleted users arrive as tombstones. A change to the POS Profile's
*Applicable for Users* table alone does not change any User: a cashier added
to the table reaches the tills once the User is saved; a cashier removed from
it is no longer sent at all, so the tills keep the old record until they pull
`cashier` from scratch (the first-pull snapshot replaces local data). To lock
a cashier out of every till right away, remove the role or disable the user.

Sales Invoice and shift payloads carry `cashier`. When it is an existing,
enabled user it is stored in the invoice's **POS Cashier** (`crenya_cashier`)
and as the shift's **Cashier**; otherwise the invoice field stays empty (the
shift falls back to the device user) and the sync event's note names the
till's cashier. The document owner stays the device's API user.

Security notes:

- A 4–6 digit PIN has at most one million combinations. Anyone who copies a
  till's database can brute-force the stored hashes offline, whatever the
  hash cost. Keep full-disk encryption on for every till and use 6-digit PINs.
- The PIN only unlocks the till. Sync still authenticates with the device
  user's API keys; a PIN never grants access to ERPNext.
- The hash also appears in the User's version history (visible to users who
  can read the User's versions, normally System Managers).

#### Till updates

Tills update themselves from the ERPNext site with the Tauri updater: they
ask `update.check` for a newer build and download it through
`update.download`, both authorized like every other call (enabled device,
registered to the calling user, POS Profile access).

Publishing a release:

1. Let CI build the till. For Windows it produces the NSIS installer
   (`…_x64-setup.exe`) and its signature file (`…_x64-setup.exe.sig`).
2. In ERPNext create a **Crenya POS Release**. *Version* is the till's
   version as `X.Y.Z` (for example `0.2.0`, no `v` and no pre-release suffix)
   and cannot be changed later; add *Notes* if the till should show any.
3. In *Artifacts* add one row per target: *Target* `windows-x86_64`
   (`windows-aarch64`, `darwin-aarch64`, `darwin-x86_64` and `linux-x86_64`
   are also accepted), *File*: attach the `.exe` as a **private** file (public
   attachments are refused), *Signature*: paste the whole content of the
   `.exe.sig` file. Each target can appear only once.
4. Tick **Published** and save. *Publication Date* is set to now if it is
   empty.

A till is offered the newest published release of its channel that is
greater than the version it runs (semantic comparison: `0.1.10` is newer
than `0.1.9`) and that has an artifact for its target; otherwise
`update.check` answers HTTP 204 and the till stays as it is. Unticking
*Published* withdraws a release from tills that have not installed it yet.

Channels: every release is `stable` or `beta`, and every **Crenya POS
Device** has an *Update Channel* (default `stable`, set by a System Manager on
the device). Stable tills only receive stable releases; beta tills receive
beta and stable releases, so a newer stable build also reaches them. Try a
build on a few tills by publishing it as `beta` and switching those devices
to the beta channel.

Tills verify the signature with the updater public key built into the app
before installing and reject any file that does not match, so a replaced or
corrupted installer is never installed. A release therefore only works with
installers signed by the CI signing key of the till.

#### Tests

Pure unit tests (no site needed):

```bash
cd ~/frappe-bench
PYTHONPATH=apps/crenya_pos_api env/bin/python -m unittest discover \
  -s apps/crenya_pos_api/crenya_pos_api/tests -p 'test_unit_*.py'
```

Integration tests (need a disposable site with ERPNext and this app installed;
they create `_Test Crenya …` masters with an example currency, country and
5 % inclusive tax and commit them):

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
