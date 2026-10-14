"""Credit sales on existing sites (0.13.0): the POS Profile *Credit sales* switch and the two
approval Checks, the Payment Entry's `crenya_local_id` and the shift's fields exist before anything
reads them (after_migrate runs after the patches).

Nothing is switched on: the switch defaults off, so an outlet that existed keeps today's behaviour
until its owner turns credit sales on (app.crenya.com). Safe to run again.
"""

from crenya_pos_api.setup.install import make_custom_fields, make_role


def execute():
	make_role()
	make_custom_fields()
