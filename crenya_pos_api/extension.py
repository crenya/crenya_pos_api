"""Public API for apps that extend the Crenya till protocol.

Import from this module only; everything else in crenya_pos_api is internal and may change
between releases. Register the extensions in your app's `hooks.py`:

        # pull entities: {entity name: dotted path of an EntitySpec subclass or instance}
        crenya_pos_pull_entities = {"restaurant_table": "my_app.sync.pull.TableSpec"}

        # push aggregates: {aggregate_type: dotted path of an AggregateHandler subclass}
        crenya_pos_aggregates = {"Crenya Order": "my_app.sync.orders.OrderAggregate"}

        # fn(ctx, doc) -> None: add keys to the device bootstrap dict (use your own key)
        crenya_pos_bootstrap = ["my_app.sync.bootstrap.extend"]

        # fn() -> dict: extra `features` flags of get_sync_capabilities (core flags win)
        crenya_pos_capabilities = ["my_app.sync.capabilities.features"]

        # fn(ctx, doc, data, notes) -> None: after the Sales Invoice is built, before insert;
        # `data` is the validated payload, `data["extensions"]["my_app"]` your app's raw data;
        # `data["credit_customer_as_of"]` (a sale: the customer record's `modified` the till had)
        # and `data["approval"]` (a return: {action, approved_by, approved_at}) are None when absent
        crenya_pos_invoice_extenders = ["my_app.sync.invoice.extend"]

        # fn(ctx, customer, data, notes) -> str | None: a till's new Customer, before insert;
        # `data` is the validated payload: `data["location"]` and `data["credit_facility"]` (the
        # till manager's grant, or None) included. The grant is {approved_by, approved_at} plus,
        # only when the till sent them, `credit_limit` (a validated decimal string, or None: no
        # limit row) and `credit_days` (0..365, or None: no own terms); an absent key means
        # untouched (`"credit_limit" in grant`). The outlet's precision is not known to the
        # validation: re-check with parse_limit(value, money_precision(ctx.currency)) and apply
        # with apply_customer_terms(customer, ctx.company, limit=..., days=...) (UNSET leaves a
        # part alone); parse_days is exported too. Return a Comment text
        # (added after the insert, HTML-escaped: return plain text) or None. To switch the House
        # Account on for this new customer after your own check of the approver, set
        # `customer.crenya_credit_enabled = 1` and `customer.flags[GRANT_FLAG] = "till"`: nothing
        # else lets a device past the editor check, the flag counts only inside the insert of a
        # new customer, and any other value on this path fails the event. Without an extender
        # `credit_facility` is dropped (a note on the Sync Event).
        crenya_pos_customer_extenders = ["my_app.sync.customer.extend"]

        # fn(ctx, customer, data, notes) -> str | None: the till's `Customer Credit Facility` event
        # (0.16.0: a manager enables an EXISTING customer's House Account). Runs after core's checks and
        # the approval's subject; `customer` is the saved Customer document (the same object that is then
        # saved, so a checker may set its own fields), `data` the validated payload (`customer`,
        # `credit_limit` / `credit_days` when sent, `approval` {action, subject, approved_by,
        # approved_at}). Return None to accept, or the refusal reason (a string: final, the event fails).
        # To leave an audit line, append plain text to `notes`: it becomes the Customer's Comment (else
        # core writes the credit terms change). Every checker must accept; with NO checker registered
        # every grant is refused ("No app accepts a House Account grant here"). Core then sets
        # `customer.flags[GRANT_FLAG] = TILL_ENABLE_GRANT` and saves; that flag counts only on the
        # save of an existing customer, and your own `validate` hook may read it too.
        crenya_pos_credit_facility_checkers = ["my_app.sync.credit.accept_facility"]

        # roles that may register and use a device besides Crenya POS User: a role name opens
        # every device type, {"role", "device_types"} only those types (never a retail "till"
        # unless listed). Holders are pulled as cashiers on those devices, with `roles`.
        crenya_pos_device_roles = [
                "My App Device",
                {"role": "My App Kitchen", "device_types": ["my_app_kds"]},
        ]

        # fn(names) -> {profile name: {flag: value}}: extra keys on list_pos_profiles rows
        # (core keys win; profiles the function leaves out get nothing)
        crenya_pos_profile_flags = ["my_app.sync.profiles.flags"]

        # device types besides "till" that may take credit (credit status, customer payments, the
        # customer's credit_limit on the pull); list only types that sell: not a waiter or kds
        crenya_pos_credit_device_types = ["my_app_pos"]

        # device types (of the credit device types) whose customer pull records and credit status
        # carry `credit_enabled`: the Customer's House Account facility (crenya_credit_enabled) and
        # not disabled, while the outlet sells on account; never a retail "till". Which sales the
        # flag refuses is your invoice extender's rule
        crenya_pos_credit_customer_flag_device_types = ["my_app_pos"]

        # fn() -> {status, paid_until, max_outlets, max_terminals, addons}: the plan's caps on
        # new terminals and the licence state sent to every device (the last app's wins)
        crenya_pos_entitlement = "my_app.licence.entitlement"

Names:
        EntitySpec: base class of a pull entity (keyset paging, tombstones and cursors are core's).
        AggregateHandler: base class of a push aggregate (envelope, hashing, idempotency on
                event_id, savepoint and commit per event and the Sync Event record are core's).
        DeviceContext: the authorized device of the request (`device_id`, `device_type`,
                `profile`, `company`, `currency`, `device` row, `entitlement`).
        SyncError: raise it (with one of the codes below) to fail one pushed event.
        staff_roles(profile, device_type): {user: [roles of that device type]} of the enabled
                staff of a POS Profile, the same people and roles the `cashier` pull sends
                enabled to a device of that type.
        device_enable_refusal(device_id): why enabling a disabled Crenya POS Device again is
                refused by the plan's caps (it is checked as a new terminal of its type and POS
                Profile), or None (no hook, a kds / waiter, or within the caps). It locks the
                device rows until your commit: call it first, then set `enabled` and commit.
        mode_of_payment_account(mode, company): the company's account of a Mode of Payment, or
                None when it has none (the lookup the Sales Invoice payments use).
        clean_location(value, field="location"): a customer's location as the till's rule wants
                it (one line, runs of spaces collapsed, at most 140; None when blank), or
                SyncError(validation).
        GRANT_FLAG: the `customer.flags` key that lets server code switch on the House Account of
                a new customer it creates ("till" from a customer extender, "web" from the web's
                New customer) or of an existing one (TILL_ENABLE_GRANT, set only by core's
                `Customer Credit Facility` event after the checkers' yes); never filled from a request.
        TILL_ENABLE_GRANT: the value of GRANT_FLAG on that existing customer's save.
        registry: the merged view (`entities()`, `aggregates()`, `handler(aggregate_type)`,
                `device_roles()`, ...).
        PROTOCOL_VERSION: protocol spoken by this server (2 = extension hooks).
"""

from crenya_pos_api.sync import registry
from crenya_pos_api.sync.aggregates import AggregateHandler
from crenya_pos_api.sync.cashier import staff_roles
from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, PROTOCOL_VERSION, DeviceContext
from crenya_pos_api.sync.credit import GRANT_FLAG, TILL_ENABLE_GRANT
from crenya_pos_api.sync.credit_terms import UNSET, apply_customer_terms, parse_days, parse_limit
from crenya_pos_api.sync.entitlement import device_enable_refusal
from crenya_pos_api.sync.errors import (
	DEPENDENCY_MISSING,
	INTERNAL,
	PAYLOAD_CONFLICT,
	PERMISSION,
	RETRYABLE_CODES,
	TOTAL_MISMATCH,
	VALIDATION,
	ExtensionHookError,
	SyncError,
)
from crenya_pos_api.sync.invoice_builder import mode_of_payment_account
from crenya_pos_api.sync.pull import EntitySpec
from crenya_pos_api.sync.validation import clean_location

__all__ = [
	"DEFAULT_DEVICE_TYPE",
	"DEPENDENCY_MISSING",
	"GRANT_FLAG",
	"INTERNAL",
	"PAYLOAD_CONFLICT",
	"PERMISSION",
	"PROTOCOL_VERSION",
	"RETRYABLE_CODES",
	"TILL_ENABLE_GRANT",
	"TOTAL_MISMATCH",
	"UNSET",
	"VALIDATION",
	"AggregateHandler",
	"DeviceContext",
	"EntitySpec",
	"ExtensionHookError",
	"SyncError",
	"apply_customer_terms",
	"clean_location",
	"device_enable_refusal",
	"mode_of_payment_account",
	"parse_days",
	"parse_limit",
	"registry",
	"staff_roles",
]
