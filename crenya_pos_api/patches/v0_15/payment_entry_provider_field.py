"""The Payment Entry's `crenya_service_provider` on existing sites (0.15.0, provider settlement spec
7.4): the field exists before a settlement sets it (after_migrate runs after the patches). A Link to
Crenya Service Provider where crenya_restaurant is installed, else Data (`bench_custom_fields`).

No Payment Entry is touched: the new column is empty on every existing entry. Safe to run again.
"""

from crenya_pos_api.setup.install import make_custom_fields


def execute():
	make_custom_fields()
