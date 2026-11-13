"""Maintenance is a new module. A property that saved an explicit module list
before it existed (guided setup, or the short-term-rental preset) has it off
without anyone having chosen that. Switch it on there once; an owner can
still turn it off in Settings.

An empty list already means every module, so those properties are left
alone. Idempotent: a list that already names maintenance is not touched."""

import frappe


def execute():
	if not frappe.db.has_column("Property", "enabled_modules"):
		return
	for name, raw in frappe.get_all("Property", fields=["name", "enabled_modules"], as_list=True):
		picked = [m.strip() for m in (raw or "").split(",") if m.strip()]
		if not picked or "maintenance" in picked:
			continue
		frappe.db.set_value("Property", name, "enabled_modules",
		                    ",".join([*picked, "maintenance"]), update_modified=False)
