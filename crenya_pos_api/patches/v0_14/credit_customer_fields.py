"""The Customer's House Account facility on existing sites (0.14.0, restaurant spec D6/RC14):
`crenya_credit_enabled` and `crenya_credit_changed_at` exist before anything reads them (after_migrate
runs after the patches).

Nothing is switched on: the Check defaults off, so every existing customer reads 0 and no Customer
row is saved (no `modified` moves, no till pulls a customer again for it). Safe to run again.
"""

from crenya_pos_api.setup.install import make_custom_fields


def execute():
	make_custom_fields()
