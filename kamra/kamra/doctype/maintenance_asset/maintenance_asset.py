# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

import frappe
from frappe import _
from frappe.model.document import Document
from frappe.utils import getdate


class MaintenanceAsset(Document):
	def validate(self):
		if self.room:
			room_prop = frappe.db.get_value("Room", self.room, "property")
			if room_prop and room_prop != self.property:
				frappe.throw(_("Room {0} is not in {1}.").format(self.room, self.property))
			if not self.floor:
				self.floor = frappe.db.get_value("Room", self.room, "floor")
		if self.warranty_until and self.install_date and \
				getdate(self.warranty_until) < getdate(self.install_date):
			frappe.throw(_("The warranty can't end before the asset was installed."))

	def on_trash(self):
		if frappe.db.exists("Maintenance Work Order", {"asset": self.name}):
			frappe.throw(_("This asset has work orders. Set it to Retired instead of deleting it."))
