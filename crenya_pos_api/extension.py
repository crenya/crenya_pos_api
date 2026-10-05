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
        # `data` is the validated payload, `data["extensions"]["my_app"]` your app's raw data
        crenya_pos_invoice_extenders = ["my_app.sync.invoice.extend"]

        # roles that may register and use a device besides Crenya POS User; holders are pulled as
        # cashiers and their cashier records list these roles under `roles`
        crenya_pos_device_roles = ["My App Device"]

        # fn(names) -> {profile name: {flag: value}}: extra keys on list_pos_profiles rows
        # (core keys win; profiles the function leaves out get nothing)
        crenya_pos_profile_flags = ["my_app.sync.profiles.flags"]

Names:
        EntitySpec: base class of a pull entity (keyset paging, tombstones and cursors are core's).
        AggregateHandler: base class of a push aggregate (envelope, hashing, idempotency on
                event_id, savepoint and commit per event and the Sync Event record are core's).
        DeviceContext: the authorized device of the request (`device_id`, `device_type`,
                `profile`, `company`, `currency`, `device` row).
        SyncError: raise it (with one of the codes below) to fail one pushed event.
        staff_roles(profile): {user: [device roles]} of the enabled staff of a POS Profile, the
                same people and roles the `cashier` pull sends enabled.
        registry: the merged view (`entities()`, `aggregates()`, `handler(aggregate_type)`,
                `device_roles()`, ...).
        PROTOCOL_VERSION: protocol spoken by this server (2 = extension hooks).
"""

from crenya_pos_api.sync import registry
from crenya_pos_api.sync.aggregates import AggregateHandler
from crenya_pos_api.sync.cashier import staff_roles
from crenya_pos_api.sync.context import DEFAULT_DEVICE_TYPE, PROTOCOL_VERSION, DeviceContext
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
from crenya_pos_api.sync.pull import EntitySpec

__all__ = [
	"DEFAULT_DEVICE_TYPE",
	"DEPENDENCY_MISSING",
	"INTERNAL",
	"PAYLOAD_CONFLICT",
	"PERMISSION",
	"PROTOCOL_VERSION",
	"RETRYABLE_CODES",
	"TOTAL_MISMATCH",
	"VALIDATION",
	"AggregateHandler",
	"DeviceContext",
	"EntitySpec",
	"ExtensionHookError",
	"SyncError",
	"registry",
	"staff_roles",
]
