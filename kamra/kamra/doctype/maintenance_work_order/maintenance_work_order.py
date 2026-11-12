# Copyright (c) 2026, HeyKoala and contributors
# For license information, please see license.txt

from frappe.model.document import Document

from kamra import maintenance


class MaintenanceWorkOrder(Document):
	"""One maintenance job. The rules live in kamra.maintenance so the API,
	the scheduler and the desk all go through the same ones."""

	def validate(self):
		maintenance.validate_work_order(self)

	def before_insert(self):
		maintenance.before_insert_work_order(self)

	def on_update(self):
		# also runs on insert, with no doc before save
		maintenance.after_save_work_order(self)

	def on_trash(self):
		maintenance.on_trash_work_order(self)
