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

**`erpnext` branch:** for plain ERPNext sites without erptrue_app. This app ships the expense
doctypes itself (POS Expense, POS Expense Line, POS Expense Log, POS Expense Category), so till
expenses work after `bench migrate`; see *Expenses* below.

#### What the app adds

- DocType **Crenya POS Device**: one row per till (`device_id` UUID, stable
  short code `D01`, `D02`, … used in offline numbers `OFF-D02-000123`) and its
  *Device Type* (`till` unless the device registers as something else, e.g.
  `kds`). Device ids are compared ignoring case (see *Device ids* below).
- DocType **Crenya Sync Event**: idempotency log of every pushed event
  (read-only in desk; *Aggregate Type* is free text, so other apps can add
  types). Successful events older than their aggregate's retention are
  purged by a daily job in batches of 1000: `crenya_pos_event_retention_days`
  (default 120, never less than 90) for Customer, Sales Invoice, Crenya POS
  Shift and types no installed app knows; an extension aggregate may set its
  own `retention_days`. Failed events are kept.
- DocType **Crenya POS Shift** (named after the till's shift `local_id`) with
  child table **Crenya POS Shift Payment** (mode of payment, expected,
  counted, difference, paid out): one row per cashier shift closed at a till, with
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
- DocType **Crenya POS Payment Terminal**: a card / UPI terminal tills drive
  for one mode of payment (see *Payment terminals*). System Manager and
  Accounts Manager manage them; Crenya POS User has no access (tills receive
  them through bootstrap).
- Child DocType **Crenya POS Scale Barcode Rule** (prefix, PLU length, value
  type, value decimals) on the POS Profile (see *Scale barcode rules*).
- Roles **Crenya POS User** and **Crenya POS Manager** (store controls' approver; see
  *Store controls*).
- Custom fields (created on install and on every migrate):
  - Sales Invoice: `crenya_local_id` (unique), `crenya_offline_number`, `crenya_device`,
    `crenya_shift_id` (shift of the till that sold it, in standard filter),
    `crenya_cashier` (POS Cashier: Link User, the till cashier who rang up the sale, in standard filter)
  - User: section **Crenya POS** on the *Roles & Permissions* tab with `crenya_pos_pin`
    (POS PIN, Password) and the hidden `crenya_pos_pin_hash` (see *Cashier PINs*)
  - Customer: `crenya_local_id` (unique)
  - Item: `crenya_item_name_ar` (Item Name (Arabic))
  - Item Group: `crenya_item_group_name_ar` (Item Group Name (Arabic))
  - Company: `crenya_company_name_ar`, `crenya_cr_number` (CR Number), and a collapsible
    section **Crenya POS** (after the address) with the receipt wording `crenya_tax_name`,
    `crenya_tax_id_label`, `crenya_invoice_title`, `crenya_credit_note_title`,
    `crenya_receipt_title`, `crenya_receipt_credit_note_title` (each with an `_ar` Arabic
    twin) and `crenya_receipt_qr` (Receipt QR Code: Verification link / ZATCA (KSA) /
    None); see *Tax and invoice wording*
  - POS Profile: `crenya_allow_return_without_invoice` (Allow returns without invoice
    (Crenya POS), Check, default off; see *Returns without an invoice*), and a collapsible
    section **Crenya POS Scale Barcodes** (after *Filters*) with the table
    `crenya_scale_barcode_rules` (see *Scale barcode rules*)

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
   the keys in ERPNext signs every till of that user out. Roles: **Crenya POS User** and **Sales User** (create customers). Since
   0.9.3 a till's sales need no desk role: the push checks that the device is
   registered to the user, who holds a device role for its type, at an outlet
   they may use, and then posts the Sales Invoice for the device (every ERPNext
   validation runs; only the permission checks are skipped). A return
   is accepted only against a sale of the same POS Profile. Two ERPNext checks
   look at the user's rights whatever the document says, so the roles of
   devices that post sales get read-only `PUSH_READS` rights
   (`setup/install.py`: select on Account, read on Item; this app grants its retail till's roles,
   an app with its own selling device types grants those). **Accepted
   exposure:** Item read lets such a user read every Item through the REST API,
   including fields a till does not need, such as `valuation_rate` and
   `last_purchase_rate`; no write right is given. The app's own records, Crenya
   POS Device, Crenya Sync Event and Crenya POS Shift, are written with
   `ignore_permissions` after the same authorization. Add the user
   to the POS Profile's *Applicable for Users* table, or leave that table empty
   to allow everyone. Every device call (profile list, registration,
   bootstrap, pull, push, returns, loyalty, Fawtara status, updates) requires
   the **Crenya POS User** role (or a role an installed app allows, see
   *Extending crenya_pos_api*); being signed in is not enough.

#### API summary

All methods are `POST {server}/api/method/crenya_pos_api.api.<module>.<fn>` with
`Authorization: token <api_key>:<api_secret>`, except `auth.login`, which the
till calls without auth to exchange username + password for that key pair, and
the `update.*` methods, which are `GET` (same token auth). Money and quantities are decimal
strings. The full contract (payloads, error codes, idempotency rules) is the
sync protocol document of the till: `apps/pos-desktop/docs/sync-protocol.md`.

| method | purpose |
|---|---|
| `auth.login` | username + password (+ `device_id`) → the **device user's** API key pair, `key_kind: "device"`, `pin_seal_key`; `user` is the person who signed in, who only authorises the pairing (see *Device users*); without a `device_id` the person's own key while `crenya_require_device_keys` is off (guest, POST, rate limited 10 / 5 min, desk lockout rules apply) |
| `device.rekey_device` | exchange the key of the person who owns a device (`device_id`) for its device user's key (see *Device users*; POST) |
| `sync.get_sync_capabilities` | ping, `protocol_version` (2), versions, features incl. `shifts`, `tax_templates`, `loyalty`, `promotions`, `verify_page`, `batches`, `open_returns`, `tax_wording`, `payment_terminals`, `scale_rules`, `uom_entity`, `extensions`, plus flags of installed extension apps (GET or POST) |
| `device.list_pos_profiles` | POS Profiles the user may use: `{name, company, warehouse, currency}`, plus flags of installed extension apps (`crenya_pos_profile_flags`) |
| `device.register_device` | idempotent device registration, assigns `D01`…; `device_id` compared ignoring case (see *Device ids*); optional `device_type` (lower case letters, digits, `_`, `-`; default `till`, kept when a later registration omits it); a NEW `till` / `restaurant_pos` is refused (`permission`) past the plan's caps or with its add-on off (see *Plan caps*) |
| `device.get_bootstrap` | device (incl. `device_type`), profile (incl. `allow_negative_stock` from Stock Settings, `tax_templates`, `loyalty_enabled`, `allow_return_without_invoice`, `scale_barcode_rules`), company (incl. `phone_country_code` and the tax / invoice wording), `settings.qty_precision`, `settings.rate_precision`, taxes, payment modes, `payment_terminals`, and the locale data `currency`, `phone_country_codes`, `cash_denominations`, `site_timezone`, `date_format` (System Settings, else the site default, else `null`) for the till, plus keys of installed extension apps, and `entitlement` (see *Plan caps*) |
| `sync.pull_changes` | (`pin_seal=1`: sealed PIN hashes, see *Device users*) keyset-paginated feed: `item`, `item_price`, `customer`, `stock`, `cashier`, `item_group`, `pricing_rule`, `batch`, `uom` (+ entities of installed extension apps) + tombstones |
| `sync.push_batch` | up to 50 events (Customer / Sales Invoice / Crenya POS Shift submit, + aggregates and operations of installed extension apps), one savepoint + commit per event |
| `loyalty.get_details` | redeemable loyalty points of a Customer (`device_id`, `customer`; POST) |
| `fawtara.get_status` | Fawtara status and ASP document id of up to 50 till invoices (`device_id`, `local_ids`; POST) |
| `returns.get_invoice_for_return` | original invoice (by name or offline number) with returned / returnable qty, `batch_no` / `expiry_date` per row, `taxes_and_charges` and `loyalty_amount` |
| `setup.ensure_custom_fields` | create / update the app's role and custom fields, as after a migrate (System Manager only, POST, idempotent) |
| `cashier.clear_pin` | remove a cashier's POS PIN (`user`; System Manager only, from a browser session: a call with an `Authorization` header, a till's or device's key, is refused (0.11.1); POST) |
| `update.check` | newest published release for the till (`device_id`, `target`, `current_version`); raw updater JSON or HTTP 204 (GET) |
| `update.download` | the release's installer for `target` (`release`, `target`, `device_id`; GET) |

#### Device users: a device never holds a person's API key (0.12.0)

Signing in on a till, waiter phone or kitchen display (`auth.login` with the device's id) only
**authorises the pairing**: the person is role-checked (Crenya POS User, a device role, or System
Manager), a device already registered is signed in again only by a System Manager or someone who
may use its POS Profile, and the person is kept as `paired_by` (on the Crenya POS Device and on the
device user, with a comment). The device gets the key pair of its own **device user**
`device-<device id>@<site host>`: a System User, with the one role `Crenya POS Device` (no desk
access, no DocPerm on User, Has Role or Role; `tests/test_integration_device_users`), made with
`explicit_roles` so another app's "new users get every role" rule does not widen it. The key is
kept (a second sign-in returns it), `register_device` makes the device user the device's `user`,
and `get_sync_capabilities` / `get_bootstrap` answer `key_kind` (`device` or `user`); the
capabilities' `user` stays the person who paired it (the name a till shows as signed in). The
device user is disabled when its device is deactivated (or deleted) and enabled when it is
reactivated. The devices list on app.crenya.com (`web.devices`) names the person who paired a
device, not its device user. Device users are not cashiers (the `cashier` pull, `staff_roles` and the staff lists
leave them out).

A device paired before this holds a person's key. The site setting `crenya_require_device_keys`
(site_config.json) decides what happens when such a key calls a device endpoint
(`get_device_context`; a browser session is no key):

| `crenya_require_device_keys` | a person's key on a device |
|---|---|
| off (the default) | allowed; an Error Log "a device is signed in with a person's API key" once a day per device; raw PIN hashes still go to devices that do not ask for seals |
| on | refused with error code `rekey_required` (403); old tills get no PIN hashes |

`device.rekey_device(device_id)`, called with the person's key for a device that person owns
(`device.user`; not even a System Manager takes over another's), makes the device user and answers
`{user, api_key, api_secret, key_kind, device_user, pin_seal_key}` (the key in hex); the device stores it and deletes
the person's key from its secret store. Called with a device user's key it answers the same key. The
person's own key is never rotated. A device user acts for its own device only.

**Sealed PIN hashes.** The `cashier` pull sent every device each staff member's raw
`pbkdf2_sha256` hash. A device that holds its `pin_seal_key` (answered at sign-in and re-key; an
HMAC of the site's encryption key and the device id, never stored) sends `pin_seal=1` and gets
`crenya_pin1$<key id>$<rounds>$<salt>$<HMAC of the PBKDF2 key>` (pos-core's sealed format): it checks
a PIN by deriving the PBKDF2 key and comparing seals, and the PBKDF2 key never leaves the server.
A device that starts asking for seals is sent every cashier again. Only a call made with a person's key
(an old till not yet re-keyed) gets the raw hash, and only while `crenya_require_device_keys` is off; a
device user's key without `pin_seal` gets `pin_hash: null`. Capabilities `features.device_users` and
`features.pin_seal` say the server does this.

**Rollout of device users (in this order).**

1. Before pairing or re-keying many devices, raise Frappe's new-user throttle in site_config.json,
   for example `"throttle_user_limit": 500`. Frappe refuses more than 60 new users an hour
   ("Throttled"), and every device makes one device user. Put it back afterwards.
2. Deploy crenya_pos_api 0.12.0 and crenya_restaurant 0.15.6 together, then `bench migrate`.
   `crenya_require_device_keys` stays off: every old till keeps selling on its person's key (a
   device user's key gets the PIN seal or no PIN hash, never the raw hash, so an old till signed in
   again offline-unlocks only after it is updated).
3. Update every till (restaurant 0.7.4, retail 0.9.0, waiter phones). Each re-keys itself on its
   next sync and deletes the person's key from its OS secret store. A branch master that registered
   paired tills with the person's key re-registers them (a device user acts for its own device only).
4. Watch the Error Log "a device is signed in with a person's API key" (one a day per device): when
   none is left, every device holds a device key.
5. Set `crenya_require_device_keys` to 1.
6. Rotate the API keys of every person who ever signed in at a till (System Managers first):
   the server never rotates them, and they still work until you do. Do it only after step 4.
7. Lower `throttle_user_limit` back.

A device user is never edited: `crenya_device_id` and `crenya_paired_by` are permlevel 1 (System
Manager) and a User hook refuses a change from anyone but the pairing code or a System Manager in a
browser session; it has no password (no password login, no reset), and pairing refuses (and
deletes) a device user that holds any role beyond `Crenya POS Device`.

#### What a till never pulls; what a cashier needs no desk role for (0.11.0)

- The `item` pull leaves out a **variant template** (`has_variants`) and a **serial-numbered
  item** (`has_serial_no`): ERPNext refuses either on a till's sale. The `item_price` pull leaves
  out a price for **one customer, supplier or batch** (`customer`, `supplier`, `batch_no`): the
  till would charge it to everyone. Each such row is sent as a **tombstone** instead of a record,
  so a till that already holds it drops it. Both pulls' cursors are scoped (`sellable`): a cursor
  issued before 0.11.0 restarts its records once (tombstones keep their position), so a till
  drops what it already held. An extension's `EntitySpec` can do the same with `excluded(row)`.
- A **customer** captured at the till is created for the authorised device
  (`ignore_permissions`), like the till's sales: a cashier whose only role is Crenya POS User
  needs no Customer right on the desk. The customer gets a comment naming the till.
- The **return lookup** (`returns.get_invoice_for_return`) is authorised by the device, not the
  cashier's desk roles: the invoice and its POS Profile must be the device's company's.

#### Expenses (the booking engine, 0.13.0)

What a cashier pays out of the drawer is recorded as a POS Expense and booked as one paid
Purchase Invoice. The engine moved down from crenya_restaurant and is shared by the retail and the restaurant
till: `expenses/booking.py` (the booking, its retry, the setup checks), `expenses/fields.py` (the
outlet's `crenya_expense*` POS Profile fields and their check), `expenses/reports.py` (the Z rows) and
`expenses/product.py`, the seam: one `ExpenseProduct` per product (device types, bootstrap key,
capability, the settings page named in messages, the approver roles). The engine names no product;
crenya_restaurant registers its own with `register_product`.

The retail till pushes expenses through the same handler (`expenses/sync.py`, aggregate `POS Expense`,
operations `submit`, `amend`, `cancel`, built in): the device's type picks the product. Retail tills
also pull `expense_category` and `expense_supplier` (empty while the outlet's Expenses switch is off, and for a kitchen display or waiter phone)
and, from 0.13.2, `expense_booking` (the booking state of the outlet's recent till expenses:
`{id: local_id, name, status, purchase_invoice, posting_error}`, so a Retry on app.crenya.com reaches the
till; only the outlet's expenses of the last 62 days by business date, so an older one retried on the
web is not delivered, and nothing for a kitchen display or waiter phone, which have no expense product; an older till never asks, an older server answers unknown entity and the till skips it),
send the bill photo to `crenya_pos_api.api.expenses.attach_photo`, and read the bootstrap's `expenses`
block with `features.retail_expenses`, all only on a site with POS Expense (shipped by this app on
the erpnext branch, so after its migrate). A retail
outlet's approvers are the holders of Crenya POS Manager on the POS Profile's *Applicable for Users*
(empty: every holder).

**The doctypes on the erpnext branch.** POS Expense, POS Expense Line, POS Expense Log and POS
Expense Category are this app's (`crenya_pos_api/crenya_pos_api/doctype/pos_expense*`, module
*Crenya Pos Api*), taken from erptrue_app under the same names so the engine's by-name lookups are
unchanged. Left out: erptrue_app's legacy `erptrue_expense` link to *ERPTrue Expense* and its
"booked once" check; every row here is a till row (`local_id`) booked as a Purchase Invoice.
Permissions: System Manager full; Accounts Manager and Accounts User read POS Expense (the engine
writes it); Accounts Manager manages POS Expense Category, Accounts User reads it. A site that
already has erptrue_app's doctypes of the same names is not a target of this branch (the two apps
would claim the same DocTypes); use `main` there.

**Setting up expenses in the ERPNext desk (erpnext branch).** On each retail outlet's **POS Profile**,
open the collapsible **Expenses** section (custom fields this app makes on install and every migrate):
tick *Expenses on the Till*, pick the *Cash Supplier* (a Supplier for payees who are not suppliers),
optionally *Expense Suppliers* (a Supplier Group offered on the till), *Expense VAT* (a Purchase Taxes
and Charges Template of the outlet's company) and *Manager PIN to Add an Expense*. Then add at least
one **POS Expense Category** (desk: `/app/pos-expense-category`, ideally with an *Expense Account* of
the company), and check that the company has a *Default Payable Account* and *Default Expense
Account*, the POS Profile has a Cash-type mode of payment with an account for the company, and Stock
Settings has a *Default Unit of Measure*. Messages that name the settings page say
`POS Profile → Expenses`. No entitlement hook is needed: without one nothing is capped.

crenya_restaurant before 0.15.7 still hooks `POS Expense`, `expense_category` and `expense_supplier`.
A hook entry that repeats one of these built-ins is ignored and logged once, so this release can be
deployed first: the restaurant tills keep working through the built-in handler (the restaurant's
approver roles are read from the installed crenya_restaurant until it registers its product).

#### Customer payments (credit sales, 0.13.0)

A till takes a customer's payment on account as the push aggregate `Customer Payment` (`sync/customer_payment.py`),
booked as one submitted Payment Entry (Receive) of the till's company: allocated to the customer's
outstanding Sales Invoices oldest first (`posting_date`, then `creation`), each up to its outstanding amount, the
remainder unallocated (an advance). The result adds `payment` {allocated, unallocated, invoices}. A resend is
idempotent by `event_id` and by the till's `local_id`, kept in the Payment Entry's unique `crenya_local_id`
(NULL on every entry made elsewhere, never an empty string; looked up before anything is booked). A payment
that cannot be booked (a mode of payment without an account for the company, a customer outside the outlet) is a
refused sync event: it shows on app.crenya.com's Refused page with the account hint and Retry books it once.
The outlet's credit switch only governs new credit sales; a receipt is taken whatever it says.

#### Device ids

A device id is the device's UUID. Ids that differ only in case are the same
device: `register_device` and every call that takes a `device_id` find the
stored device whatever the case of the id sent, and always answer with the
stored spelling (`device_id` in the registration and the bootstrap).

A registration whose id differs only in case from a registered device may only
find that device again. It is refused (`permission`) when it comes from another
user, for another POS Profile, or with another `device_type`, and otherwise
gets the stored record back with nothing changed: it never renames the device,
moves it to another user or profile, or changes its type. A device that sends
its id exactly as it registered it (every Crenya POS till does) re-registers as
before, including a rename or another user taking the till over.

So an outlet hub that registers devices for others cannot be tricked into
rewriting a live till's record (or its own) with an upper-cased copy of its id.

#### Plan caps (entitlement)

A workspace app (erptrue_app) answers the `crenya_pos_entitlement` hook with
the plan's caps and licence state:

```json
{"status": "Active", "paid_until": "2026-12-31", "max_outlets": 2, "max_terminals": 5,
 "addons": {"restaurant": 1, "pos": 0}}
```

`status` is `""`, `Active`, `Trial`, `Grace` or `Suspended`; `paid_until` is
`YYYY-MM-DD` or `null`; a cap of `0` is unlimited. Without the hook there is no
restriction and `entitlement` is `null`. A hook that fails or answers another
shape is logged (once per 10 minutes) and treated as no hook: it never blocks.

- **Terminals** are enabled devices of type `till` (add-on `pos`) or
  `restaurant_pos` (add-on `restaurant`). `kds`, `waiter` and other types are
  never counted and never refused. **Outlets** are the distinct POS Profiles of
  the enabled terminals.
- Only a NEW device id registering as a terminal is checked: its add-on must be
  on (`New POS (Tauri) is not switched on for this workspace.` /
  `New Restaurant (Tauri) is not switched on for this workspace.`), the
  terminals must be below `max_terminals` (`This plan allows {n} tills. Ask the
  owner to upgrade.`) and, on a POS Profile not counted yet, the outlets below
  `max_outlets` (`This plan allows {n} outlets. Ask the owner to upgrade.`).
  The refusal is a `permission` error.
- A terminal already registered (any case of its id) re-registers at its POS
  Profile at any cap; moving to a profile no other terminal uses needs a free
  outlet slot. A registered `kds` / `waiter` re-registering as a terminal is
  checked like a new one. The check is repeated when a concurrent registration
  forces a retry. Disabling a device frees its slot. Nothing here stops a sale.
- **Enabling a disabled device again** is checked like a new terminal of its
  stored type and POS Profile, with the same messages: an app that offers a
  Reactivate action calls `extension.device_enable_refusal(device_id)` and
  shows the answer, or sets `enabled = 1` when it is `None` (no hook, a `kds` /
  `waiter`, or within the caps). It reads the device rows with a locking read
  (`SELECT … FOR UPDATE`) held until the caller commits, so two devices enabled
  at once cannot both take the last slot: call it before locking the device
  row, then save and commit.
  One race remains: an enable at the same moment as a new registration can
  end one over the cap (registration reads plainly); follow-up: a `GET_LOCK`
  around check-and-write.
- Every reply to a device call (`get_bootstrap`, `pull_changes`, `push_batch`,
  returns, loyalty, Fawtara) carries `entitlement` beside `message`;
  `get_bootstrap` also has it inside `message`. The hook is read once per
  call (`DeviceContext.entitlement`). Tills that do not know the key
  ignore it.

#### Shifts

When a cashier closes a shift the till queues one `push_batch` event with
`aggregate_type = "Crenya POS Shift"` and `operation = "submit"`. The server
creates the **Crenya POS Shift** (with its payments table) and records how
many Sales Invoices with `crenya_shift_id = local_id` it already has.
Since 0.10.0 the payload may also carry `expense_count` (integer >= 0, the
expenses recorded for the shift, cancelled ones included) and, on each cash
row of `payments`, `paid_out` (decimal >= 0, the cash paid out of the drawer).
Both are stored read-only (`Crenya POS Shift.expense_count`, `Crenya POS Shift
Payment.paid_out`); a missing key counts as 0, and `expected` is stored as the
till sent it (already net of paid out), never recomputed. The
event is idempotent on `event_id` and on the shift `local_id`, like Customer
events, and depends on nothing: invoices of the shift may be pushed before or
after it. Each Sales Invoice payload may carry `shift_local_id` (string or
null, optional for older tills), stored in `crenya_shift_id` so ERPNext can
report per shift. The per-event result is the standard one with
`doctype: "Crenya POS Shift"`, `docstatus: 0` and `totals: null`.

Since 0.11.0 the payload may also carry the shift's **store controls** (see *Store controls*):

```json
"store_controls": {
  "line_voids": {"count": 3, "amount": "1.250"},
  "bill_clears": {"count": 1, "amount": "4.000"},
  "no_sales": 2,
  "approvals": 5
}
```

Counts are whole numbers >= 0; amounts decimal strings >= 0 with at most the currency's
decimals (`1.2345` for OMR is refused: `validation`). They are stored read-only in the shift's
*Store controls* section (`line_voids_count`, `line_voids_amount`, `bill_clears_count`,
`bill_clears_amount`, `no_sales_count`, `approvals_count`). A missing object or key is 0, so
a crenya pos till before 0.9.0 (e.g. 0.8.0) closes its shifts unchanged.

#### Store controls (approvals, 0.11.0)

Which till actions need a manager is set per POS Profile, in its collapsible **Store controls**
section, and reaches retail tills (device type `till`) in the bootstrap. Code:
`crenya_pos_api/sync/approvals.py`; `APPROVAL_ACTIONS` is the one table of the actions (the
custom fields, the bootstrap and the web settings form are made from it):

| action | POS Profile field (Check, default on) |
|---|---|
| `price_change` | `crenya_approve_price_change` |
| `discount_over_limit` | `crenya_approve_discount_over_limit` |
| `line_void` | `crenya_approve_line_void` |
| `bill_clear` | `crenya_approve_bill_clear` |
| `return` | `crenya_approve_return` |
| `open_return` | `crenya_approve_open_return` |
| `no_sale` | `crenya_approve_no_sale` |
| `stock_mode` | `crenya_approve_stock_mode` |
| `settings` | `crenya_approve_settings` |

`crenya_max_discount_percent` (Percent) is the most a cashier discounts alone; blank or 0 is no
limit. The profile's `allow_discount_change` and `allow_rate_change` keep their meaning, what a
cashier may do alone; beyond that an action switched on here is allowed with the PIN of a
**Crenya POS Manager** (a role this app creates, no desk access), or at once when one is signed
in at the till. The bootstrap of a retail till carries:

```json
"approvals": {
  "approver_roles": ["Crenya POS Manager"],
  "max_discount_percent": "10",
  "required": ["price_change", "discount_over_limit", "line_void", "bill_clear", "return",
               "open_return", "no_sale", "stock_mode", "settings"]
}
```

`max_discount_percent` is `null` for no limit; `required` lists the switched-on actions in the
table's order. Crenya POS Manager opens a retail till like Crenya POS User (a core device role
of type `till` only), so the `cashier` pull sends it in a manager's `roles`; the cashier cursor
of a till restarts once because its role set changed. `approvals.RETAIL_PRIVILEGES` maps the
staff privileges to roles: cashier = Crenya POS User, manager = Crenya POS User + Crenya POS
Manager. The approval Checks default to on for POS Profiles created from 0.11.0. Patch
`v0_11.store_controls` creates the role and fields on existing sites and switches every approval
**off** for the profiles that existed before: their tills ask for nothing new until the owner
turns actions on.

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
| `invoice_title`, `invoice_title_ar` | *Invoice Title* (`crenya_invoice_title`, `_ar`): A4 / full tax invoice | `Tax Invoice`; `_ar` `null` |
| `credit_note_title`, `credit_note_title_ar` | *Credit Note Title* (`crenya_credit_note_title`, `_ar`): A4 / full credit note | `Credit Note`; `_ar` `null` |
| `receipt_title`, `receipt_title_ar` | *Receipt Title* (`crenya_receipt_title`, `_ar`): thermal receipt | `invoice_title` / `invoice_title_ar` |
| `receipt_credit_note_title`, `receipt_credit_note_title_ar` | *Receipt Credit Note Title* (`crenya_receipt_credit_note_title`, `_ar`): thermal return receipt | `credit_note_title` / `credit_note_title_ar` |
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

Each language falls back on its own: a blank *Receipt Title (Arabic)* prints
the *Invoice Title (Arabic)*, even when *Receipt Title* is set.

Examples: Saudi Arabia: Tax ID Label `VAT No.`, Invoice Title `Tax Invoice` /
`فاتورة ضريبية`, Receipt Title `Simplified Tax Invoice` / `فاتورة ضريبية
مبسطة`, Receipt QR Code *ZATCA (KSA)*. UAE: Tax ID
Label `TRN`. India: install India Compliance (GSTIN, HSN/SAC come from its
fields; a CGST + SGST template prints both rows). Oman: nothing to set
(`VAT`, `Tax ID`, `Tax Invoice`, verification link), or Tax ID Label `VATIN`
and Receipt Title `Simplified Tax Invoice`.

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
- `settings.rate_precision` (integer): decimals ERPNext rounds a Sales Invoice
  Item *Rate* to (a property setter on the field included, else the currency
  precision), as `frappe.get_precision` resolves it. A row's amount is
  `flt(rate * qty)` at the money precision, so a till that derives a rate from
  a target amount (for example a shared dish split over several lines) rounds
  the rate to this before checking the amount. Older tills ignore it.
- Totals tolerance: `site_config.crenya_pos_total_tolerance` when set, else
  10 × the smallest unit of the invoice currency's precision (`0.10` with 2
  decimals, `0.010` with 3).

To add denominations: *Crenya Cash Denomination* → New, pick the *Currency*,
enter the *Value* (face value, e.g. `20` or `0.5`), a *Label* as printed on
the note or coin (shown on the till), *Kind* `note` or `coin`, and keep
*Enabled* ticked. A currency may have each value once. Untick *Enabled* to
hide a denomination; tills pick up changes on their next bootstrap.

#### Loyalty points

Uses ERPNext's own **Loyalty Program**. Setup (on app.crenya.com: Shops → Loyalty,
`web_admin.save_loyalty_program`): create a Loyalty Program for
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
`item_group` sends the whole Item Group tree (`name, item_group_name_ar,
parent_item_group, lft, rgt, modified`; `item_group_name_ar` is the Item
Group's *Item Group Name (Arabic)*, `null` when empty, like the item's
`item_name_ar`). ERPNext renumbers `lft` / `rgt` of other groups without
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

#### Whole-number units (UOM)

ERPNext refuses a fractional quantity in a UOM that has *Must be Whole Number*
ticked ("Quantity cannot be a fraction"). Tills check it before the sale:
`pull_changes` entity `uom` sends every UOM as `{name, must_be_whole_number
(bool), modified}`, keyset-paginated on the UOM's `modified` like the other
entities; deleted UOMs arrive as tombstones. `sync.get_sync_capabilities`
announces it with `features.uom_entity`.

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

#### Payment terminals

Tills can take card and UPI payments on a terminal and book them to a mode of
payment. Create one **Crenya POS Payment Terminal** per terminal (Accounts
Manager or System Manager):

- *Label* (shown on the till's pay button), *Provider*, *Company*, *Mode of
  Payment* and, optionally, *POS Profile* (blank: every POS Profile of the
  company). The mode of payment needs an account for the company (Mode of
  Payment → *Accounts*) and, with a POS Profile, must be in that profile's
  payment methods. Only the chosen provider's settings are shown:
  - **UPI QR** (India, works offline): *UPI ID (VPA)* in the form
    `name@bank` (required) and *Payee Name* (optional). The
    till shows a `upi://pay` QR code with the amount; the cashier confirms
    the payment.
  - **Pine Labs Cloud** (needs internet): *Environment* (UAT / Production),
    *Merchant ID*, *Store ID*, *Client ID* (required, as issued by Pine
    Labs), *User ID*, *Security Token* (required, stored encrypted),
    *Allowed Payment Mode* (Pine Labs code, for example `1` or `1,10`) and
    *Auto Cancel (Minutes)* (0 to 1440, blank: Pine Labs' default).
  - **Geidea**, **Network International ECR**, **Bank ECR**: *Host*, *Port*,
    *Terminal ID*, *Notes*. Stored now; tills offer them once their adapters
    are integrated.
- Records are named `<Label> - <POS Profile>`, or `<Label> - <company
  abbreviation>` for every profile (a number is added when the name is
  taken).

Bootstrap sends `payment_terminals`: the enabled terminals of the device's
company whose POS Profile is the device's (or blank) and whose mode of payment
the profile offers, as `{name, provider, mode_of_payment, label,
needs_internet, config}`. `provider` is `upi_qr`, `pinelabs_cloud`, `geidea`,
`network_ecr` or `bank_ecr`; `needs_internet` is false for `upi_qr` and
`bank_ecr` (shop LAN) and true for the others. `config` holds only the
provider's own settings:

| provider | `config` keys |
|---|---|
| `upi_qr` | `upi_vpa`, `upi_payee_name` |
| `pinelabs_cloud` | `environment` (`uat` / `production`), `merchant_id`, `store_id`, `client_id`, `user_id`, `security_token`, `allowed_payment_mode`, `auto_cancel_minutes` (null when blank) |
| `geidea`, `network_ecr`, `bank_ecr` | `host`, `port` (null when blank), `terminal_id` |

The Pine Labs security token is decrypted only for the bootstrap of a
registered, enabled device of the calling user (the same check as every
device call); the record shows it masked and tills keep it in the operating
system's keychain.

Sale and return payloads may carry `reference_no` on a payment row (terminal
RRN / UTR / approval code, at most 140 characters), stored in the Sales
Invoice payment row's *Reference No*. Payment rows of the same mode of payment
are merged into one ERPNext row as before; their distinct references are
joined with `, ` (together at most 140 characters, else `validation`).

`sync.get_sync_capabilities` announces this with `features.payment_terminals`.

#### Scale barcode rules

Scales print EAN-13 labels: prefix, PLU (the item's barcode or item code),
the weight or price, check digit. Set the rules per POS Profile in the
section **Crenya POS Scale Barcodes** (`crenya_scale_barcode_rules`), one row
per prefix:

- *Prefix*: 1 to 3 digits (for example `21`); each prefix once, and no prefix
  may start another one (`2` and `21` are refused).
- *PLU Length*: 1 to 10 digits; prefix and PLU together must stay under 12
  digits, leaving room for the value.
- *Value Type*: Weight or Price.
- *Value Decimals*: blank (weight: 3, grams to kilograms; price: the
  currency's precision) or 0 to 6.

Bootstrap sends them as `profile.scale_barcode_rules: [{prefix, plu_length,
value_type ("weight" / "price"), value_decimals (null when blank)}]` in table
order. Non-empty rules replace the till's local scale settings; an empty table
leaves them as configured on the till. `sync.get_sync_capabilities` announces
this with `features.scale_rules`.

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
The user needs the **Crenya POS User** role, or a role an extension app allows
on the device's type (`crenya_pos_device_roles`, e.g. Restaurant Manager on a
restaurant device, never on a retail till) (and, if the POS Profile's
*Applicable for Users* table is filled, must be listed there). On save the
PIN must be 4 to 6 digits 0-9, otherwise the save is refused. The server
replaces it with a salted hash (`pbkdf2_sha256$120000$<salt>$<key>`,
PBKDF2-HMAC-SHA256, 16-byte random salt, 32-byte key, base64) in the hidden
field `crenya_pos_pin_hash` and empties the PIN field, so the PIN is stored
nowhere in readable or decryptable form. The PIN field therefore always looks
empty; leaving it empty keeps the current PIN, typing a new one replaces it.
Desk users can also set their own PIN under *My Settings*. To remove a PIN, a
System Manager calls `crenya_pos_api.api.cashier.clear_pin` with `user` from a
signed-in browser session (never with an API key: see *Staff and accounts* below); on the
server, `bench --site <site> execute crenya_pos_api.sync.cashier.clear_user_pin --args "['cashier1@store.om']"`.

Tills receive cashiers through `pull_changes` entity `cashier`:
`{name, full_name, enabled, pin_hash, roles, modified}`, keyset-paginated on the
User's `modified`. It contains the holders of a **device role of the pulling
device's type** (Crenya POS User and the `crenya_pos_device_roles` roles for
that type; on a retail till only Crenya POS User and roles hooked for every
type), limited to the profile's *Applicable for Users* when that table is not
empty, and users that have a PIN. `enabled` is 1 only for an enabled user who
still has such a role and may use the profile; `pin_hash` is null when no PIN
is set and for every record with `enabled: 0`. `roles` lists those roles the
user holds, Crenya POS User first and then the hooked roles in hook order, for
example `["Crenya POS User", "Restaurant Manager"]` on a restaurant device. No other ERPNext role of the user
is ever sent. A device uses it to decide who may approve a manager-only action
(which role counts as a manager is up to the extension app). `roles` is `[]`
when `enabled` is 0. Adding or removing a role saves the User, so the change
reaches the tills on their next pull. Removing the role or disabling the
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

**Central releases are the default** (0.11.0). A build is published once to the static
manifest `https://dl.crenya.com/pos/releases.json` (`constants.CENTRAL_RELEASES_URL`; site
config `crenya_pos_releases_url` overrides it, an empty value turns it off), not to each site.
Code: `crenya_pos_api/sync/central.py`, product-neutral (`central.Product`; retail is
`central.RETAIL`, other apps pass their own):

```json
{"schema": 1, "product": "crenya pos",
 "releases": [{"version": "0.9.0", "channel": "stable", "pub_date": "2026-10-13T08:00:00Z",
               "notes": "...",
               "updater": {"windows-x86_64": {"url": "https://dl.crenya.com/pos/0.9.0/...exe",
                                              "signature": "<the .sig file>",
                                              "sha256": "<64 hex>", "size": 15700000}},
               "installers": [{"platform": "windows", "label": "Windows", "label_ar": "ويندوز",
                               "url": "https://dl.crenya.com/pos/0.9.0/...exe",
                               "sha256": "<64 hex>", "size": 15700000}]}]}
```

- Only retail tills (device type `till`) are offered central retail builds; other device
  types see the site's own releases only, as before.
- Its releases are offered like the site's own rows (named `central:<version>`; the same
  channel, target and version rules). A published Crenya POS Release of the **same version
  wins**, when it has a file for the till's target.
- The manifest is fetched within one 3 s deadline (connect and every read together; an installer 120 s), at most 1 MB, and kept 10
  minutes; its last good copy is kept 7 days. Unreachable or invalid, the site's own releases
  are still offered, and one warning with the URL and the reason goes to the `crenya_pos_api`
  logger, at most once per 10 minutes. An invalid release, target or installer is left out,
  the rest kept.
- `check` still answers the workspace's own `download` URL (the till's `Authorization` stays
  on its workspace). `download` of a `central:` release fetches it from the **manifest's own
  host** only (scheme, host and port; no redirect followed), reads at most `size` plus one
  byte, and refuses it (`not_found`) unless its sha256 and size are the manifest's. A verified
  artifact is kept on the site by its sha256 (`private/crenya_pos_api/release_artifacts/`, not
  a File, so never in the site's backups) and served from there afterwards. The signature is
  passed through byte for byte: the till's own key stays the only trust anchor.
- `update.downloads` (a signed-in system user; portal users and Guest are refused) answers the newest stable central release with
  installers, `{version, pub_date, notes, installers: [{platform, label, label_ar, url,
  sha256, size}]}`, or `{installers: []}`: the Downloads page of app.crenya.com.

Publishing a site-only release (a hotfix or a beta for one workspace; it overrides a
central release of the same version):

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

#### Web admin core

`crenya_pos_api/web/` holds the product-neutral pieces of the till admin on app.crenya.com,
shared by the retail web admin and other apps' (crenya_restaurant imports them):

- `forms.settings_form(fields, owner_editable, owner_only=frozenset(), read_only=frozenset())`
  builds the settings form from custom field definitions (Section Breaks open sections), and
  `forms.settings_change_problem(values, fields, owner_editable, owner_only, read_only)` says why
  a change may not be written (`("invalid" | "refused", message)` or None). Which settings only a
  System Manager changes, and which are never written from the web, are the caller's.
- `context.WebAdminContext(approver, outlet=None)`: the signed-in user changing an outlet (its
  company and currency come with it); `log(doc)` comments "Changed from app.crenya.com by …".
  Also `refuse`, `invalid`, `checked`, `parse_flag`, `check_modified` (a stale save is refused
  with Frappe's TimestampMismatchError), `setting_value`, `is_system_manager`.
- `setup_checks.money_setup_checks(currency)`: rows `{key, status, label, detail, fix}` saying
  whether ERPNext's precision and rounding of the currency are the tills' (Banker's Rounding at
  the currency's decimals), read live; `check(...)` builds a product's own rows.

- `devices.devices_answer(outlet, can_manage, type_order, type_labels)` and
  `devices.set_device_enabled(ctx, device, enabled, modified)`: an outlet's devices with their
  open shift and the plan's terminal cap, and deactivating (never while in use: a shift open or
  seen within 24 hours) or reactivating one within the plan's caps. Never deletes a device.
- `retail_checks.retail_setup_checks(profile)`: a shop's own setup checks (below).
- `staff.save_web_staff(approver, doc, privilege, pin, outlets, outlet_users, privilege_roles)`
  and its rules (`pin_problem`, `listing_changes`, `privilege_of`, `role_changes`,
  `web_staff_user`, ...): a user's till privilege, 6-digit PIN and outlets, with each product's
  `{privilege: roles}` map (retail: `sync.approvals.RETAIL_PRIVILEGES`).

#### Shops on app.crenya.com (0.11.0)

`crenya_pos_api.api.web_admin` answers app.crenya.com's Shops pages with the method names and
answer shapes of `crenya_restaurant.api.web_admin`, so the web's till-admin pages read both
products alike. A shop is a POS Profile that is not a restaurant outlet. Who manages one: a
System Manager every shop; a **Crenya POS Manager** the shops whose Applicable for Users lists
them (or lists nobody). For a shop, reading is managing; a cashier gets no outlets. Every write
runs after that check with `ignore_permissions`, keeps the DocType's own validation, needs the
`modified` it was read with where the record has one, and comments "Changed from app.crenya.com
by <user>" on the record.

**Staff and accounts are changed from app.crenya.com alone (0.11.1).** `staff_get`, `staff_save`,
`staff_clear_pin` and `cashier.clear_pin` refuse (403) a call that carries an `Authorization`
header (`web.context.require_web_session`): app.crenya.com calls with the signed-in user's own
session cookie, while a till or another device signs with its user's API key. So no device, and
no manager PIN typed at one, ever changes a user, a PIN or a role, even when the till is signed
in as a System Manager.

| method | what |
|---|---|
| `outlets()` | the shops the user manages: `{outlets: [{name, outlet_name, company, currency, can_manage}]}` |
| `devices(outlet)`, `set_device_enabled(outlet, device, enabled, modified)` | the shop's tills; deactivate / reactivate within the plan's caps |
| `downloads()` | the newest stable crenya pos installers (the central manifest) |
| `outlet_settings(outlet)`, `save_outlet_settings(outlet, values, modified)` | the settings form and `setup_checks` (money checks + the shop's own) |
| `scale_rules(outlet)`, `save_scale_rules(outlet, rows, modified)` | the scale barcode rules, checked by the till's own rules |
| `denominations(outlet)`, `save_denominations(outlet, rows)` | the cash denominations of the shop's currency (System Manager writes) |
| `terminals(outlet)`, `save_terminal(outlet, terminal)` | payment terminals; the security token is write-only |
| `company_wording(outlet)`, `save_company_wording(outlet, values, modified)` | the company's receipt wording (System Manager writes) |
| `staff_get(user)`, `staff_save(user, privilege, pin, outlets)`, `staff_clear_pin(user)` | a user's shop till privilege (`cashier`: Crenya POS User; `manager`: also Crenya POS Manager), 6-digit PIN and shops; System Manager only, never Administrator or Guest, never System Manager given; the answer lists the `privileges` offered |
| `day_closes(from_date, to_date, outlet)`, `day_close(shift)` | the shop's shifts (Z reports): sections `sales`, `payments` (per mode: expected, counted, difference), `returns`, `store_controls`, `paid_out`; every row has `kind` money or count, money as strings at the currency's decimals |
| `refused_sales(outlet)`, `retry_sale(name)` | what the shop's tills pushed and ERPNext refused (newest 200), with a hint in English and Arabic; Retry asks the till to send it again (`retry_pending` until the till has, so Retry is offered again once a re-push is refused) |
| `credit_customers(outlet)`, `save_credit_limit(outlet, customer, credit_limit, modified)` | the shop's customers with a credit limit or a balance (`credit_limit` / `available` null = "No limit"; `limit_source` says whether the limit is the customer's own, its group's or the company's; over the limit first; at most 200, `more` says it was cut); read by the shop's managers. The edit is a System Manager's or an Accounts Manager's only (a shop manager approves at the till, he does not set the limit): it writes the Customer's own *Credit Limit* row for the shop's company (0 or blank clears it, a bypass flag on the row is kept; clearing it is not "no limit" when the group or the company has one), comments old and new value and the web user |
| `credit_shops()` | the shops the user may open the Credit page of: `{shops: [{name, outlet_name, company, currency, credit_view, credit_enabled, can_edit}]}`. A System Manager or an Accounts Manager gets every shop of the companies they may use (User Permission on Company honoured, none = all) without being a Crenya POS Manager of it, and may edit (`can_edit`); a shop manager the shops they manage, read only; a cashier none. `credit_customers` and `save_credit_limit` follow the same rule; every other Shops method still needs the user to manage the shop |
| `ensure_cash_supplier(outlet)`, `expenses_not_booked(outlet?)`, `till_expense(name)`, `retry_expense_booking(name)` | the shop's till expenses: the Cash Supplier button, the Not booked list scoped to the shops the user manages, one expense with its lines and log, and Retry (the engine is told the product); a manager needs no POS Expense right |
| `promotions(outlet)`, `save_promotion(outlet, promotion)`, `set_promotion_enabled(outlet, name, enabled, modified)` | the shop's promotions: Pricing Rules of its company for its price list (or for every price list, a System Manager's), only the shapes a till evaluates (item code / group / brand; % or amount off, a rate, or buy X get Y, optionally repeating; min / max qty, dates, priority); coupons refused |
| `loyalty_programs(outlet)`, `save_loyalty_program(outlet, program)` | ERPNext Loyalty Programs of the shop's company with their tiers; created and changed here, never deleted or renamed (end one with its To Date) |

Every Shops method needs a browser session (`require_web_session`, taken in `scope.manage` / `managed_shops`): a till's or a person's API key is refused; the credit and expense methods also need the user to be permitted the shop's company (a User Permission on Company is honoured). The settings form also carries the *Expenses* and *Credit sales* sections (the Cash Supplier, supplier group and expense VAT links are a System Manager's), `expense_setup_checks` and `credit_setup_checks` beside `setup_checks`; the Z report gains an `on_account` section (when the shift sold or received on account) and an `expenses` section (one row each, then the total).

The settings form is ERPNext's own POS Profile fields (read from its meta: price list,
customer, discount and rate changes, warehouse, taxes, cost center, rounding, write-off and the
read-only item groups) and this app's custom fields (returns without an invoice, *Store
controls*). The taxes template, cost center and write-off links are a System Manager's.

**Retry (the `sync_retry` pull entity, 0.11.0).** Retry sets the refused event's
`retry_requested_at`. A till that pulls `sync_retry` gets `{event_id, retry_requested_at}` for
each of its own events asked to be retried and not processed since, and pushes that event again
unchanged (the push is idempotent); once processed again the event comes as a tombstone. Every
device type may pull it, and `features.sync_retry` says the server has it; a till that never
pulls it is unaffected.

Setup checks (`web/retail_checks.py`), each `{key, status, label, detail, fix}`:
`price_list` (a selling list in the shop's currency), `tax_template` (of the company; none is a
warning), `warehouse` (not a group), `default_customer`, `payment_modes` (each with an account of
the company), `write_off` (when the rounded total is on), `cashiers` (an enabled user with a
PIN who works there) and `approver` (a Crenya POS Manager with a PIN; missing when an approval
is switched on, else a warning).

#### Extending crenya_pos_api

Another Frappe app (`required_apps = ["crenya_pos_api"]`) can add pull
entities, push aggregates with their own operations, bootstrap keys,
capability flags, invoice data, device roles and POS Profile flags, without
changing this app. Everything is registered in the other app's `hooks.py` and read through
`frappe.get_hooks()` once per request. Built-ins always come first and cannot
be replaced. When two apps register the same entity or aggregate name, the
app installed later wins.

Import only from `crenya_pos_api.extension` (`EntitySpec`, `AggregateHandler`,
`DeviceContext`, `SyncError` and the error codes, `ExtensionHookError`,
`registry`, `staff_roles`, `device_enable_refusal`, `PROTOCOL_VERSION`).
Everything else is internal.
`staff_roles(profile, device_type)` returns `{user: [roles]}` of the enabled
staff of a POS Profile for a device of that type: the same people and roles the
`cashier` pull sends with `enabled: 1` to such a device.
`device_enable_refusal(device_id)` is the plan-cap check for enabling a
disabled device again (see *Plan caps*).

| hook | shape | called |
|---|---|---|
| `crenya_pos_pull_entities` | `{"<entity>": "<dotted path of an EntitySpec subclass or instance>"}` | `sync.pull_changes(entity=...)`. Keyset paging, cursor and tombstones are handled by core. |
| `crenya_pos_aggregates` | `{"<aggregate_type>": "<dotted path of an AggregateHandler subclass or instance>"}` | `sync.push_batch` for events of that `aggregate_type` |
| `crenya_pos_bootstrap` | `["<dotted path of fn(ctx, doc) -> None>"]` | end of `device.get_bootstrap`. `doc` is the bootstrap dict; add your own top-level key. |
| `crenya_pos_capabilities` | `["<dotted path of fn() -> dict>"]` | `sync.get_sync_capabilities`. The dict is merged into `features`; keys already there (core flags, or an earlier hook's) are kept. |
| `crenya_pos_invoice_extenders` | `["<dotted path of fn(ctx, doc, data, notes) -> None>"]` | after the Sales Invoice is built (rows, taxes, payments, totals checked), before `insert` and `submit` |
| `crenya_pos_device_roles` | `["<role name>", {"role": "<role name>", "device_types": ["<device_type>", ...]}]` | roles that may sign in, register and use a device, besides **Crenya POS User**. A plain role name opens every device type. A `{role, device_types}` entry opens only those device types. Holders are pulled as cashiers on those devices, and their cashier records list these roles in `roles`. See *Device roles and device types* below. |
| `crenya_pos_profile_flags` | `["<dotted path of fn(names) -> {profile name: {flag: value}}>"]` | `device.list_pos_profiles`. Called once with the names of the listed profiles; the flags are added to each row. Keys already there (core keys, or an earlier hook's) are kept, and a profile the function leaves out gets nothing. Without the hook the rows are unchanged. |
| `crenya_pos_entitlement` | `"<dotted path of fn() -> dict>"` | `register_device` and every device call; the last app's wins. See *Plan caps*. Must never raise; a failure is logged and means no caps. |

**Device roles and device types.** A retail till registers without a
`device_type`, so it is stored as `till` (the Crenya POS Device field default).
A device of type `till` accepts only **Crenya POS User** and roles hooked as
plain names. A role scoped with `device_types` never opens it: its holder
cannot register a till or use one, and is not a cashier there. If the holder
has a PIN, the till's `cashier` pull sends them with `enabled: 0`, no
`pin_hash` and `roles: []`. This keeps tills that are already deployed safe
when an app with staff roles (for example crenya_restaurant) is installed on
the same site. The device type is checked as follows:

- **Sign-in** (`auth.login`) and `list_pos_profiles` happen before a device
  is known, so they accept a role of any device type.
- **`register_device`** checks the roles of the type the device will have:
  the `device_type` sent, else the type already stored for the device, else
  `till`.
- **Every call that loads a device** (`get_device_context`: bootstrap, pull,
  push, returns and so on) checks the roles of the stored type.
- **`staff_roles(profile, device_type)`** uses the same roles.

Crenya POS User opens every type and cannot be narrowed by a hook. A role
listed by two apps opens the union of their types.

The `cashier` cursor carries the device type's role set (`s`). When that set
changes, the next pull sends every cashier again with the new `enabled` and
`roles`. That happens when a device registers again with another type, or
when an app adds a role for the type. Tombstones keep their position. A
cursor from before 0.8.0 has no `s`, so a till pulls its cashiers once again
after the upgrade.

```python
crenya_pos_device_roles = [
	"Shift Supervisor",  # every device type, retail tills included
	{"role": "Restaurant Captain", "device_types": ["restaurant_pos", "waiter"]},
]
```

`ctx` is the `DeviceContext` of the authorized device (`device_id`,
`device_type`, `profile`, `company`, `currency`, and the `device` row).
`notes` is a list of strings stored on the event's Crenya Sync Event. A hook
path that cannot be imported, or an object of the wrong kind, raises
`ExtensionHookError` naming the hook and the path. Its code is `internal` and
it is retryable, so a pushed event fails with a retryable error and the till
keeps it until the server is fixed.

**AggregateHandler.** Core keeps the protocol: envelope checks, payload hash,
idempotency on `event_id` (the same event twice gives one effect; the same
`event_id` with another payload gives `payload_conflict`), one savepoint and
commit per event, the Crenya Sync Event and the result shape. The handler
supplies:

| member | meaning |
|---|---|
| `operations` | tuple of the envelope `operation` values it accepts (default `("submit",)`). Anything else is a `validation` error. Lower case letters, digits and `_`. |
| `doctype` | DocType of the documents it returns (default: the aggregate type) |
| `retention_days` | days its successful Sync Events are kept (default `None` = the site setting) |
| `validate(operation, payload) -> dict` | structural checks of the raw payload. Raise `SyncError(VALIDATION, "...")` naming the field. |
| `apply(ctx, operation, data, notes) -> Document` | write the change and return the document |
| `find_by_local_id(local_id) -> str \| None` | idempotency fallback for an event whose document exists although the till never saw the answer. It runs for every operation, so return `None` (the default) for aggregates changed by several operations. |
| `result_fields(doc) -> dict` | `doctype`, `name`, `docstatus`, `modified`, `totals`, `fawtara_status` of the result (the default fills them from the document, with `totals` and `fawtara_status` empty) |

Raise `SyncError` with `DEPENDENCY_MISSING` (retried by the till) or
`VALIDATION` (final) to fail one event. Nothing the handler wrote is kept
then.

**Invoice payload additions (protocol 2, all optional).**
- `extensions: {"<app>": {...}}`: carried in the hashed payload, so a retried
  event has exactly the same data. Core only checks that it is an object.
  Extenders read `data["extensions"].get("<app>")` and validate their own
  entry.
- Line `notes`: a string, at most 500 characters.
- Line `parent_line_no`: the `line_no` of another line, for example a priced
  modifier under its dish. Loops are rejected.

Core validates `notes` and `parent_line_no` and passes them to extenders in
`data["items"]`; it does not write them to the invoice itself. Each Sales
Invoice Item row has `row.flags.crenya_line_no`, the payload line it came
from.

**Devices.** `register_device` accepts `device_type` (default `till`). It is
on `DeviceContext.device_type` and in the bootstrap `device` block, so an app
can send different data to, for example, a kitchen display.
`get_sync_capabilities` reports `protocol_version: 2` and
`features.extensions: true`. Protocol 1 tills keep working unchanged.

Worked example: an app `my_restaurant` with dining tables (pull), orders that
are created, then get items added and are closed (push), table data on the
settlement invoice, and a device role for an outlet service user.

```python
# my_restaurant/hooks.py
required_apps = ["crenya_pos_api"]

crenya_pos_pull_entities = {"restaurant_table": "my_restaurant.sync.TableSpec"}
crenya_pos_aggregates = {"Restaurant Order": "my_restaurant.sync.OrderAggregate"}
crenya_pos_bootstrap = ["my_restaurant.sync.extend_bootstrap"]
crenya_pos_capabilities = ["my_restaurant.sync.features"]
crenya_pos_invoice_extenders = ["my_restaurant.sync.extend_invoice"]
crenya_pos_device_roles = ["Restaurant Device"]
```

```python
# my_restaurant/sync.py
import frappe

from crenya_pos_api.extension import (
	DEPENDENCY_MISSING,
	VALIDATION,
	AggregateHandler,
	EntitySpec,
	SyncError,
)


class TableSpec(EntitySpec):
	doctype = "Restaurant Table"
	fields = ("name", "table_name", "seats", "pos_profile")

	def filters(self, table, ctx):
		return [table.pos_profile == ctx.profile.name]


class OrderAggregate(AggregateHandler):
	doctype = "Restaurant Order"
	operations = ("create", "add_items", "close")
	retention_days = 30

	def validate(self, operation, payload):
		table = payload.get("table")
		if operation == "create" and not isinstance(table, str):
			raise SyncError(VALIDATION, "table is required")
		return {"local_id": payload["local_id"], "table": table, "items": payload.get("items") or []}

	def apply(self, ctx, operation, data, notes):
		if operation == "create":
			order = frappe.get_doc(
				{
					"doctype": "Restaurant Order",
					"local_id": data["local_id"],
					"table": data["table"],
					"device": ctx.device_id,
				}
			)
			order.insert(ignore_permissions=True)
			return order
		name = frappe.db.get_value("Restaurant Order", {"local_id": data["local_id"]})
		if not name:
			raise SyncError(DEPENDENCY_MISSING, "the order has not synced yet")
		order = frappe.get_doc("Restaurant Order", name)
		if operation == "add_items":
			for item in data["items"]:
				order.append("items", item)
		else:
			order.status = "Closed"
		order.save(ignore_permissions=True)
		return order


def extend_bootstrap(ctx, doc):
	doc["my_restaurant"] = {"kitchen_display": ctx.device_type == "kds"}


def features():
	return {"restaurant_orders": True}


def extend_invoice(ctx, doc, data, notes):
	ext = data["extensions"].get("my_restaurant")
	if not ext:
		return
	doc.remarks = f"Table {ext['table']}"
	lines = {line["line_no"]: line for line in data["items"]}
	for row in doc.items:
		line = lines[row.flags.crenya_line_no]
		if line["notes"]:
			row.description = f"{row.item_name} ({line['notes']})"
	notes.append(f"table {ext['table']}")
```

A till then pushes, for example,
`{"aggregate_type": "Restaurant Order", "operation": "add_items", "local_id": "<order uuid>", ...}`
and settles the bill with a normal Sales Invoice event whose payload has
`"extensions": {"my_restaurant": {"table": "T4"}}`.

To test an extension without installing it as an app, answer the hooks from a
patched `frappe.get_hooks` (and call `registry.clear_cache()` around it), as
`crenya_pos_api/tests/extension_dummy.py` does for this app's own tests.

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

The expenses booking tests (`tests/test_integration_expenses_booking`, `test_integration_expenses_retail`,
`test_integration_web_admin_expenses`) run on any migrated test site on the erpnext branch, since this
app ships POS Expense (a site without it fails their setup); `TestExpensesOff` / `TestRetailExpensesOff`
only run before the migrate and skip otherwise. The expense doctype JSONs have a pure check,
`python -m unittest discover -s crenya_pos_api/tests -p 'test_doctype_*.py'` (no site, no Frappe).

#### Contributing

This app uses `pre-commit` for code formatting and linting. Please [install pre-commit](https://pre-commit.com/#installation) and enable it for this repository:

```bash
cd apps/crenya_pos_api
pre-commit install
```

#### License

mit
