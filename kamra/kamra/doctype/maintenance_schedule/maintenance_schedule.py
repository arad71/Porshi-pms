# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from kamra import maintenance


class MaintenanceSchedule(Document):
	def validate(self):
		maintenance.validate_schedule(self)
