"""The Customer's `crenya_location` on existing sites (0.15.0, provider settlement spec NC2): the
field exists before a till's Customer event or the pull reads it (after_migrate runs after the
patches).

No Customer is touched: the new column is empty on every existing customer. Safe to run again.
"""

from crenya_pos_api.setup.install import make_custom_fields


def execute():
	make_custom_fields()
