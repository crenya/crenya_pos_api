"""The Payment Entry's `crenya_shift_id`, `crenya_cashier` and `crenya_device` on existing sites
(0.16.0, house account phase 2): the fields exist before a till payment sets them (after_migrate
runs after the patches). The transactions report reads them.

No Payment Entry is touched: the new columns are empty on every existing entry. Safe to run again.
"""

from crenya_pos_api.setup.install import make_custom_fields


def execute():
	make_custom_fields()
