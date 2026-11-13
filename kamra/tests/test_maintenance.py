"""Maintenance: work orders move rooms, assets and guest requests; schedules
raise planned work on their cycle."""

from __future__ import annotations

import frappe
from frappe.utils import add_days, getdate

from kamra import maintenance
from kamra.tests.audit_fixtures import (
	PROP_A, PROP_B, TODAY, AuditTestCase, ensure_user, make_property, make_room,
	make_room_type,
)

WO = "Maintenance Work Order"
TECH = "ut-maint-tech@kamra.local"
DESK = "ut-maint-desk@kamra.local"


def _hk(room: str) -> str:
	return frappe.db.get_value("Room", room, "housekeeping_status")


def _wo(prop: str, **fields):
	return frappe.get_doc({"doctype": WO, "property": prop, "subject": "UT job",
	                       **fields}).insert(ignore_permissions=True)


def _asset(prop: str, **fields):
	return frappe.get_doc({"doctype": "Maintenance Asset", "property": prop,
	                       "asset_name": "UT chiller", "category": "HVAC",
	                       **fields}).insert(ignore_permissions=True)


def _schedule(prop: str, **fields):
	return frappe.get_doc({"doctype": "Maintenance Schedule", "property": prop,
	                       "title": "UT filter clean", "frequency": "Monthly",
	                       "next_due_date": TODAY, **fields}).insert(ignore_permissions=True)


def _complete(doc, note="Fixed"):
	doc.reload()
	doc.resolution_note = note
	doc.status = "Completed"
	doc.save(ignore_permissions=True)
	doc.reload()
	return doc


class TestMaintenanceWorkOrders(AuditTestCase):
	def setUp(self):
		super().setUp()
		make_property(PROP_A)
		make_property(PROP_B)
		rt = make_room_type(PROP_A, "MNT")
		self.room = make_room(PROP_A, rt, "M101")
		self.room2 = make_room(PROP_A, rt, "M102")
		self.room_b = make_room(PROP_B, make_room_type(PROP_B, "MNT"), "M101")
		frappe.db.set_value("Room", [self.room, self.room2],
		                    {"housekeeping_status": "Clean", "occupancy_status": "Vacant"})

	def test_due_by_follows_priority(self):
		urgent = _wo(PROP_A, priority="Urgent")
		low = _wo(PROP_A, priority="Low")
		self.assertLess(urgent.due_by, low.due_by)

	def test_total_cost_adds_up(self):
		wo = _wo(PROP_A, labour_cost=100, parts_cost=40.5, vendor_cost=10)
		self.assertEqual(wo.total_cost, 150.5)

	def test_completing_needs_a_note(self):
		wo = _wo(PROP_A)
		wo.status = "Completed"
		with self.assertRaises(frappe.ValidationError):
			wo.save(ignore_permissions=True)

	def test_room_from_another_property_refused(self):
		with self.assertRaises(frappe.ValidationError):
			_wo(PROP_A, room=self.room_b)

	def test_room_out_of_order_while_open_then_dirty(self):
		wo = _wo(PROP_A, room=self.room, take_room_out_of_order=1)
		self.assertEqual(_hk(self.room), "Out of Order")
		wo.reload()
		wo.status = "In Progress"
		wo.save(ignore_permissions=True)
		self.assertEqual(_hk(self.room), "Out of Order")
		_complete(wo)
		self.assertEqual(_hk(self.room), "Dirty")

	def test_room_stays_out_while_another_order_holds_it(self):
		a = _wo(PROP_A, room=self.room, take_room_out_of_order=1)
		b = _wo(PROP_A, room=self.room, take_room_out_of_order=1, subject="UT second")
		_complete(a)
		self.assertEqual(_hk(self.room), "Out of Order")
		_complete(b)
		self.assertEqual(_hk(self.room), "Dirty")

	def test_cancel_gives_room_back(self):
		wo = _wo(PROP_A, room=self.room, take_room_out_of_order=1)
		wo.reload()
		wo.status = "Cancelled"
		wo.save(ignore_permissions=True)
		self.assertEqual(_hk(self.room), "Dirty")

	def test_occupied_room_not_taken_out(self):
		frappe.db.set_value("Room", self.room, "occupancy_status", "Occupied")
		with self.assertRaises(frappe.ValidationError):
			_wo(PROP_A, room=self.room, take_room_out_of_order=1)
		# logging the job without closing the room is fine
		_wo(PROP_A, room=self.room)
		self.assertEqual(_hk(self.room), "Clean")

	def test_moving_order_to_another_room_frees_the_first(self):
		wo = _wo(PROP_A, room=self.room, take_room_out_of_order=1)
		wo.reload()
		wo.room = self.room2
		wo.save(ignore_permissions=True)
		self.assertEqual(_hk(self.room), "Dirty")
		self.assertEqual(_hk(self.room2), "Out of Order")

	def test_only_supervisors_take_rooms_out(self):
		ensure_user(TECH, ["Maintenance"])
		frappe.set_user(TECH)  # nosemgrep: frappe-setuser -- acting as a technician
		with self.assertRaises(frappe.PermissionError):
			frappe.get_doc({"doctype": WO, "property": PROP_A, "subject": "UT tech",
			                "room": self.room, "take_room_out_of_order": 1}).insert()

	def test_asset_under_repair_then_back_in_service(self):
		asset = _asset(PROP_A, room=self.room)
		wo = _wo(PROP_A, asset=asset.name)
		self.assertEqual(wo.room, self.room)  # taken from the asset
		self.assertEqual(wo.category, "HVAC")
		wo.status = "In Progress"
		wo.save(ignore_permissions=True)
		self.assertEqual(frappe.db.get_value("Maintenance Asset", asset.name, "status"),
		                 "Under Repair")
		_complete(wo)
		a = frappe.db.get_value("Maintenance Asset", asset.name,
		                        ["status", "last_serviced_on"], as_dict=True)
		self.assertEqual(a.status, "In Service")
		self.assertTrue(a.last_serviced_on)

	def test_completed_cannot_jump_to_cancelled(self):
		wo = _complete(_wo(PROP_A))
		wo.status = "Cancelled"
		with self.assertRaises(frappe.ValidationError):
			wo.save(ignore_permissions=True)

	def test_reopen_clears_finish(self):
		wo = _complete(_wo(PROP_A))
		self.assertTrue(wo.completed_on)
		wo.status = "Open"
		wo.save(ignore_permissions=True)
		wo.reload()
		self.assertFalse(wo.completed_on)

	def test_api_create_by_room_number_and_status(self):
		ensure_user(DESK, ["Front Desk"])
		frappe.set_user(DESK)  # nosemgrep: frappe-setuser -- acting as the desk
		out = maintenance.create_work_order(PROP_A, "Tap dripping", category="Plumbing",
		                                    room="M101")
		name = out["work_order"]
		self.assertEqual(frappe.db.get_value(WO, name, "room"), self.room)
		maintenance.set_work_order_status(name, "Completed", note="Washer replaced",
		                                  parts_cost=12)
		row = frappe.db.get_value(WO, name, ["status", "total_cost", "completed_by"], as_dict=True)
		self.assertEqual(row.status, "Completed")
		self.assertEqual(row.total_cost, 12)
		self.assertEqual(row.completed_by, DESK)

	def test_summary_counts(self):
		_wo(PROP_A, room=self.room, take_room_out_of_order=1)
		_complete(_wo(PROP_A, labour_cost=50))
		s = maintenance.maintenance_summary(PROP_A)
		self.assertGreaterEqual(s["open"], 1)
		self.assertGreaterEqual(s["rooms_out_of_order"], 1)
		self.assertGreaterEqual(s["cost_this_month"], 50)


class TestMaintenanceTickets(AuditTestCase):
	def setUp(self):
		super().setUp()
		make_property(PROP_A, enabled_modules="")
		rt = make_room_type(PROP_A, "MNT")
		self.room = make_room(PROP_A, rt, "M201")

	def _ticket(self, **fields):
		return frappe.get_doc({"doctype": "Service Ticket", "property": PROP_A,
		                       "subject": "AC not cooling", "category": "Maintenance",
		                       "priority": "High", "status": "Open", "room": self.room,
		                       **fields}).insert(ignore_permissions=True)

	def test_ticket_in_progress_raises_work_order_and_completion_resolves_it(self):
		t = self._ticket()
		t.status = "In Progress"
		t.save(ignore_permissions=True)
		name = frappe.db.get_value(WO, {"service_ticket": t.name})
		self.assertTrue(name)
		self.assertFalse(frappe.db.exists("Housekeeping Task", {
			"room": self.room, "task_type": "Maintenance", "status": "Pending"}))
		_complete(frappe.get_doc(WO, name), note="Gas topped up")
		t.reload()
		self.assertEqual(t.status, "Resolved")
		self.assertIn("Gas topped up", t.resolution_note)

	def test_one_work_order_per_ticket(self):
		t = self._ticket()
		a = maintenance.work_order_for_ticket(t)
		b = maintenance.work_order_for_ticket(t)
		self.assertEqual(a, b)

	def test_without_the_module_the_old_housekeeping_task_stays(self):
		frappe.db.set_value("Property", PROP_A, "enabled_modules", "front-desk,admin,housekeeping")
		t = self._ticket()
		t.status = "In Progress"
		t.save(ignore_permissions=True)
		self.assertFalse(frappe.db.exists(WO, {"service_ticket": t.name}))
		self.assertTrue(frappe.db.exists("Housekeeping Task", {
			"room": self.room, "task_type": "Maintenance"}))


class TestMaintenanceSchedules(AuditTestCase):
	def setUp(self):
		super().setUp()
		make_property(PROP_A, enabled_modules="")
		self.at(TODAY, "kamra.maintenance")

	def test_next_due_cycles(self):
		self.assertEqual(maintenance.next_due("2031-01-31", "Monthly", 1), getdate("2031-02-28"))
		self.assertEqual(maintenance.next_due(TODAY, "Weekly", 2), getdate(add_days(TODAY, 14)))
		self.assertEqual(maintenance.next_due(TODAY, "Quarterly", 1), getdate("2031-06-12"))
		self.assertEqual(maintenance.next_due(TODAY, "Yearly", 0), getdate("2032-03-12"))

	def test_due_schedule_raises_one_order_and_rolls_forward(self):
		s = _schedule(PROP_A)
		maintenance.generate_preventive_work_orders()
		orders = frappe.get_all(WO, filters={"schedule": s.name}, fields=["name", "work_type"])
		self.assertEqual(len(orders), 1)
		self.assertEqual(orders[0].work_type, "Preventive")
		s.reload()
		self.assertEqual(getdate(s.next_due_date), getdate("2031-04-12"))
		self.assertEqual(s.last_work_order, orders[0].name)
		# a second run the same day raises nothing more
		maintenance.generate_preventive_work_orders()
		self.assertEqual(frappe.db.count(WO, {"schedule": s.name}), 1)

	def test_lead_days_raise_early(self):
		s = _schedule(PROP_A, next_due_date=add_days(TODAY, 3), lead_days=5)
		maintenance.generate_preventive_work_orders()
		self.assertEqual(frappe.db.count(WO, {"schedule": s.name}), 1)

	def test_not_yet_due_waits(self):
		s = _schedule(PROP_A, next_due_date=add_days(TODAY, 10), lead_days=2)
		maintenance.generate_preventive_work_orders()
		self.assertEqual(frappe.db.count(WO, {"schedule": s.name}), 0)

	def test_open_order_blocks_the_next(self):
		s = _schedule(PROP_A, frequency="Daily")
		maintenance.generate_preventive_work_orders()
		frappe.db.set_value("Maintenance Schedule", s.name, "next_due_date", TODAY)
		maintenance.generate_preventive_work_orders()
		self.assertEqual(frappe.db.count(WO, {"schedule": s.name}), 1)

	def test_long_idle_schedule_raises_one_not_a_backlog(self):
		s = _schedule(PROP_A, frequency="Weekly", next_due_date=add_days(TODAY, -70))
		maintenance.generate_preventive_work_orders()
		self.assertEqual(frappe.db.count(WO, {"schedule": s.name}), 1)
		s.reload()
		self.assertGreater(getdate(s.next_due_date), getdate(TODAY))

	def test_inactive_or_module_off_raises_nothing(self):
		off = _schedule(PROP_A, active=0)
		maintenance.generate_preventive_work_orders()
		self.assertEqual(frappe.db.count(WO, {"schedule": off.name}), 0)
		frappe.db.set_value("Property", PROP_A, "enabled_modules", "front-desk,admin")
		on = _schedule(PROP_A)
		maintenance.generate_preventive_work_orders()
		self.assertEqual(frappe.db.count(WO, {"schedule": on.name}), 0)

	def test_schedule_asset_must_be_same_property(self):
		make_property(PROP_B)
		asset = _asset(PROP_B)
		with self.assertRaises(frappe.ValidationError):
			_schedule(PROP_A, asset=asset.name)


class TestMaintenanceTypes(AuditTestCase):
	"""Each kind of maintenance and what triggers it."""

	def setUp(self):
		super().setUp()
		make_property(PROP_A, enabled_modules="")
		self.at(TODAY, "kamra.maintenance")
		self.gen = _asset(PROP_A, asset_name="UT generator", category="Electrical",
		                  meter_unit="Run Hours")

	def test_every_type_is_offered(self):
		options = frappe.get_meta(WO).get_field("work_type").options.split("\n")
		self.assertEqual(tuple(options), maintenance.WORK_TYPES)

	def test_emergency_is_always_urgent(self):
		wo = _wo(PROP_A, work_type="Emergency", priority="Low")
		self.assertEqual(wo.priority, "Urgent")

	def test_statutory_needs_a_certificate(self):
		wo = _wo(PROP_A, work_type="Statutory", subject="UT lift certification")
		wo.resolution_note = "Inspected"
		wo.status = "Completed"
		with self.assertRaises(frappe.ValidationError):
			wo.save(ignore_permissions=True)
		wo.certificate_ref = "LIFT-2031-118"
		wo.save(ignore_permissions=True)
		self.assertEqual(wo.status, "Completed")

	def test_statutory_certificate_sets_next_visit(self):
		s = _schedule(PROP_A, work_type="Statutory", frequency="Yearly",
		              title="UT fire certificate")
		name = maintenance.raise_from_schedule(s)
		wo = frappe.get_doc(WO, name)
		self.assertEqual(wo.work_type, "Statutory")
		wo.certificate_ref = "FIRE-77"
		wo.certificate_expiry = "2032-06-30"
		_complete(wo)
		self.assertEqual(getdate(frappe.db.get_value("Maintenance Schedule", s.name,
		                                             "next_due_date")), getdate("2032-06-30"))

	def test_schedule_refuses_unplanned_types(self):
		for kind in ("Corrective", "Emergency", "Condition-based", "Improvement"):
			with self.assertRaises(frappe.ValidationError):
				_schedule(PROP_A, work_type=kind)

	def test_planned_types_carry_onto_the_order(self):
		for kind in ("Routine", "Inspection", "Guest Room PM", "Seasonal"):
			s = _schedule(PROP_A, work_type=kind, title=f"UT {kind}")
			name = maintenance.raise_from_schedule(s)
			self.assertEqual(frappe.db.get_value(WO, name, "work_type"), kind)

	def test_predictive_raised_by_meter_reading(self):
		s = _schedule(PROP_A, trigger="Meter Reading", asset=self.gen.name,
		              meter_interval=250, title="UT generator service")
		s.reload()
		self.assertEqual(s.work_type, "Predictive")
		self.assertEqual(s.next_meter_due, 250)
		# the daily calendar run leaves meter schedules alone
		maintenance.generate_preventive_work_orders()
		self.assertEqual(frappe.db.count(WO, {"schedule": s.name}), 0)
		self.assertEqual(maintenance.record_asset_reading(self.gen.name, reading=200)["raised"], [])
		raised = maintenance.record_asset_reading(self.gen.name, reading=260)["raised"]
		self.assertEqual(len(raised), 1)
		self.assertEqual(frappe.db.get_value(WO, raised[0], "work_type"), "Predictive")
		self.assertEqual(frappe.db.get_value("Maintenance Schedule", s.name, "next_meter_due"), 500)

	def test_reading_cannot_go_backwards_without_reset(self):
		maintenance.record_asset_reading(self.gen.name, reading=100)
		with self.assertRaises(frappe.ValidationError):
			maintenance.record_asset_reading(self.gen.name, reading=50)
		maintenance.record_asset_reading(self.gen.name, reading=5, reset=1)
		self.assertEqual(frappe.db.get_value("Maintenance Asset", self.gen.name, "meter_reading"), 5)

	def test_meter_schedule_needs_a_metered_asset(self):
		plain = _asset(PROP_A, asset_name="UT sofa", category="Furniture & Fixtures")
		with self.assertRaises(frappe.ValidationError):
			_schedule(PROP_A, trigger="Meter Reading", asset=plain.name, meter_interval=10)

	def test_poor_condition_raises_condition_based_once(self):
		out = maintenance.record_asset_reading(self.gen.name, condition="Critical",
		                                       note="Oil leak")
		self.assertEqual(len(out["raised"]), 1)
		wo = frappe.get_doc(WO, out["raised"][0])
		self.assertEqual((wo.work_type, wo.priority), ("Condition-based", "Urgent"))
		again = maintenance.record_asset_reading(self.gen.name, condition="Poor")
		self.assertEqual(again["raised"], [])
		_complete(wo, note="Gasket replaced")
		self.assertEqual(frappe.db.get_value("Maintenance Asset", self.gen.name, "condition"), "Good")

	def test_fair_condition_raises_nothing(self):
		self.assertEqual(maintenance.record_asset_reading(self.gen.name, condition="Fair")["raised"], [])

	def test_summary_groups_open_by_type(self):
		_wo(PROP_A, work_type="Emergency")
		_wo(PROP_A, work_type="Improvement")
		by = maintenance.maintenance_summary(PROP_A)["open_by_type"]
		self.assertGreaterEqual(by.get("Emergency", 0), 1)
		self.assertGreaterEqual(by.get("Improvement", 0), 1)


class TestMaintenanceModuleSwitch(AuditTestCase):
	def test_patch_turns_maintenance_on_for_saved_lists(self):
		from kamra.patches.v47 import enable_maintenance_module
		make_property(PROP_A, enabled_modules="front-desk,admin,housekeeping")
		make_property(PROP_B, enabled_modules="")
		enable_maintenance_module.execute()
		self.assertEqual(frappe.db.get_value("Property", PROP_A, "enabled_modules"),
		                 "front-desk,admin,housekeeping,maintenance")
		# empty already means every module
		self.assertFalse(frappe.db.get_value("Property", PROP_B, "enabled_modules"))
		# running it again changes nothing
		enable_maintenance_module.execute()
		self.assertEqual(frappe.db.get_value("Property", PROP_A, "enabled_modules"),
		                 "front-desk,admin,housekeeping,maintenance")

	def test_short_term_rentals_get_maintenance(self):
		from kamra.property_presets import STR_MODULES
		self.assertIn("maintenance", STR_MODULES)
