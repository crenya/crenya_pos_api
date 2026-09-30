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
- Role **Crenya POS User**.
- Custom fields (created on install and on every migrate):
  - Sales Invoice: `crenya_local_id` (unique), `crenya_offline_number`, `crenya_device`,
    `crenya_shift_id` (shift of the till that sold it, in standard filter),
    `crenya_cashier` (POS Cashier: Link User, the till cashier who rang up the sale, in standard filter)
  - User: section **Crenya POS** on the *Roles & Permissions* tab with `crenya_pos_pin`
    (POS PIN, Password) and the hidden `crenya_pos_pin_hash` (see *Cashier PINs*)
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
| `sync.get_sync_capabilities` | ping, versions, features incl. `shifts`, `tax_templates`, `loyalty` (GET or POST) |
| `device.list_pos_profiles` | POS Profiles the user may use |
| `device.register_device` | idempotent device registration, assigns `D01`… |
| `device.get_bootstrap` | profile (incl. `allow_negative_stock` from Stock Settings, `tax_templates`, `loyalty_enabled`), company (incl. `phone_country_code`), taxes, payment modes for the till |
| `sync.pull_changes` | keyset-paginated feed: `item`, `item_price`, `customer`, `stock`, `cashier` + tombstones |
| `sync.push_batch` | up to 50 events (Customer / Sales Invoice / Crenya POS Shift submit), one savepoint + commit per event |
| `loyalty.get_details` | redeemable loyalty points of a Customer (`device_id`, `customer`; POST) |
| `returns.get_invoice_for_return` | original invoice (by name or offline number) with returned / returnable qty |
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

Bootstrap `company.phone_country_code` is the dialling code of the company's
country (`+968` Oman, `+971` United Arab Emirates, `+966` Saudi Arabia,
`+974` Qatar, `+973` Bahrain, `+965` Kuwait, `+91` India, `+92` Pakistan,
`+880` Bangladesh, `+63` Philippines, `+20` Egypt; anything else `+968`).

#### Loyalty points

Uses ERPNext's own **Loyalty Program**. Setup: create a Loyalty Program for
the company (collection rules, *Conversion Factor* = currency value of one
point, *Expense Account* and *Cost Center* for redemptions, *From Date* not in
the future) and set it as the Customer's *Loyalty Program* (or tick *Auto Opt
In* so new customers are enrolled). Bootstrap `profile.loyalty_enabled` is
true when an active program exists for the company.

- **Earning**: a till sale to an enrolled customer carries the customer's
  program, so ERPNext books the earned points on submit exactly as for a desk
  invoice. Till returns do not carry the program, so points earned on the
  original sale are not reduced by a return.
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
  keeps one zero payment row. `validation` when: the invoice is a return, the
  customer is the POS Profile's default customer, the customer has no loyalty
  program (of the company), the program is inactive or has no expense
  account, `amount` is more than points × conversion factor or more than the
  tolerance below it, `amount` exceeds the invoice total, or the customer
  does not have enough points.

`sync.get_sync_capabilities` announces both with `features.tax_templates`
and `features.loyalty`.

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
