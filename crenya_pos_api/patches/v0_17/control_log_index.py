"""The Crenya POS Control Log's unique (device, local_id) on existing sites (0.17.0). The DocType's
`on_doctype_update` adds it whenever the DocType is synced; this patch makes sure of it after the
model sync. Each event's (device, seq) is the unique column `device_seq` of Crenya POS Control Event,
made by the model sync itself. Safe to run again."""

from crenya_pos_api.crenya_pos_api.doctype.crenya_pos_control_log.crenya_pos_control_log import (
	on_doctype_update,
)


def execute():
	on_doctype_update()
