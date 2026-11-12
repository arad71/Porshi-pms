"""Maintenance: work orders, the asset register and preventive schedules.

A Maintenance Work Order is one job: a broken AC, a lift inspection, the
monthly filter clean. It says where (room, asset or a public area), who has
it (a staff member or an outside vendor), when it is due, what was done and
what it cost.

Three things move with a work order so nobody has to remember them:

  - Rooms. A work order flagged "take room out of order" makes the room Out
    of Order (not sold) while it is open. When the last such order on the
    room is completed or cancelled, the room goes to Dirty so housekeeping
    turns it before it is sold again. An occupied room is never taken out
    of order from here: move the guest first.
  - Assets. An open order on an asset puts it Under Repair; completing it
    puts it back In Service and stamps last_serviced_on.
  - Guest requests. A Maintenance ticket that goes In Progress raises a
    work order for it, and completing that work order resolves the ticket
    with what was done.

Every kind of maintenance has a type, and the types that need a trigger
have one:

  Corrective       a fault reported by staff or a guest (tickets land here)
  Emergency        a breakdown: always Urgent, on the Urgent deadline
  Preventive       planned on a calendar cycle by a Maintenance Schedule
  Predictive       planned by a meter: a schedule due every N run hours,
                   cycles, km... raised when a reading crosses it
  Condition-based  raised when an asset is recorded in Poor or Critical
                   condition
  Routine          daily / weekly rounds (a schedule)
  Inspection       checks and audits (a schedule or by hand)
  Statutory        legal certifications - fire, lift, boiler, electrical;
                   cannot be completed without a certificate / report number
  Guest Room PM    the full guest-room check, room by room (a schedule)
  Seasonal         pool opening, AC changeover (a yearly schedule)
  Improvement      upgrades and projects, by hand

Calendar schedules are checked every morning: the due ones raise a work
order, one open order per schedule at a time, and roll their next due date
forward by the cycle. Meter schedules are checked whenever a reading is
recorded on their asset.
"""

from __future__ import annotations

import frappe
from frappe import _
from frappe.utils import (
	add_days,
	add_months,
	add_to_date,
	cint,
	flt,
	get_datetime,
	getdate,
	now_datetime,
	nowdate,
)

from kamra.authz import ADMIN, assert_property_access, require_roles

MODULE = "maintenance"
WO = "Maintenance Work Order"

MAINT_TECH = "Maintenance"
MAINT_SUPERVISOR = "Maintenance Supervisor"

OPEN_STATES = ("Open", "In Progress", "On Hold")
DONE_STATES = ("Completed", "Cancelled")

# who works maintenance (read the board, update their orders)
MAINT_ROLES = (MAINT_TECH, MAINT_SUPERVISOR, "Kamra Agent")
# who may report a fault (raise a work order)
REPORT_ROLES = (*MAINT_ROLES, "Front Desk", "Housekeeping", "Housekeeping Supervisor")
# who plans: schedules, assets, costs
MANAGER_ROLES = (MAINT_SUPERVISOR, "Kamra Agent")
# who may take a room out of sale - the room supervisors in kamra.api plus
# the maintenance supervisor
OOO_ROLES = {MAINT_SUPERVISOR, "Housekeeping Supervisor", "Front Desk",
             "Kamra Agent", *ADMIN}

# priority -> hours until the order counts as overdue. Each property sets
# its own in Settings -> Maintenance (maintenance_sla_<priority>).
SLA_HOURS = {"Urgent": 4, "High": 24, "Medium": 72, "Low": 168}

WORK_TYPES = ("Corrective", "Emergency", "Preventive", "Predictive", "Condition-based",
              "Routine", "Inspection", "Statutory", "Guest Room PM", "Seasonal", "Improvement")
# what a schedule may raise - faults, emergencies and projects are never planned
PLANNED_TYPES = ("Preventive", "Predictive", "Routine", "Inspection", "Statutory",
                 "Guest Room PM", "Seasonal")
METER = "Meter Reading"
BAD_CONDITION = {"Poor": "High", "Critical": "Urgent"}

# frequency -> (unit, size) for rolling a schedule forward
FREQUENCY = {
	"Daily": ("days", 1), "Weekly": ("days", 7), "Monthly": ("months", 1),
	"Quarterly": ("months", 3), "Half-yearly": ("months", 6), "Yearly": ("months", 12),
}


# --- small helpers -----------------------------------------------------------

def sla_hours(property: str | None, priority: str | None) -> int:
	from kamra.settings import get_setting
	p = priority if priority in SLA_HOURS else "Medium"
	return int(get_setting(property, f"maintenance_sla_{p.lower()}", SLA_HOURS[p]))


def module_on(property: str | None) -> bool:
	"""Does this property run the Maintenance app? Empty setting = all."""
	if not property:
		return False
	raw = frappe.db.get_value("Property", property, "enabled_modules")
	picked = [m.strip() for m in (raw or "").split(",") if m.strip()]
	return not picked or MODULE in picked


def next_due(date, frequency: str | None, every: int | None):
	"""The due date one cycle after `date`."""
	unit, size = FREQUENCY.get(frequency or "Monthly", FREQUENCY["Monthly"])
	step = size * max(cint(every) or 1, 1)
	return getdate(add_days(date, step) if unit == "days" else add_months(date, step))


def _roles() -> set:
	return set(frappe.get_roles())


def _can_take_room_out() -> bool:
	return frappe.session.user == "Administrator" or bool(OOO_ROLES & _roles())


def _open_ooo_orders(room: str, exclude: str | None = None) -> list[str]:
	filters = {"room": room, "take_room_out_of_order": 1,
	           "status": ("in", list(OPEN_STATES))}
	if exclude:
		filters["name"] = ("!=", exclude)
	return frappe.get_all(WO, filters=filters, pluck="name")


def _log(action: str, doc, rationale: str):
	try:
		from kamra.savings import log_action
		log_action(action_type=action, reference_doctype=doc.doctype,
		           reference_name=doc.name, property=doc.property, rationale=rationale)
	except Exception:
		frappe.log_error(title=f"Maintenance activity not logged: {doc.name}")


# --- work order rules (called from the doctype controller) -------------------

def validate_work_order(doc):
	"""Everything that must hold before a work order is saved."""
	if doc.room:
		room_prop = frappe.db.get_value("Room", doc.room, "property")
		if room_prop and room_prop != doc.property:
			frappe.throw(_("Room {0} is not in {1}.").format(doc.room, doc.property))
	if doc.asset:
		a = frappe.db.get_value("Maintenance Asset", doc.asset, ["property", "room", "category"],
		                        as_dict=True)
		if a and a.property != doc.property:
			frappe.throw(_("Asset {0} is not in {1}.").format(doc.asset, doc.property))
		if a and not doc.room and a.room:
			doc.room = a.room
		if a and (not doc.category or doc.category == "Other") and a.category:
			doc.category = a.category

	doc.total_cost = flt(doc.labour_cost) + flt(doc.parts_cost) + flt(doc.vendor_cost)
	if doc.work_type not in WORK_TYPES:
		doc.work_type = "Corrective"
	if doc.work_type == "Emergency":
		doc.priority = "Urgent"

	before = doc.get_doc_before_save() if not doc.is_new() else None
	if before and before.status in DONE_STATES and doc.status in DONE_STATES \
			and before.status != doc.status:
		frappe.throw(_("A {0} work order can only be reopened, not changed to {1}.").format(
			_(before.status).lower(), _(doc.status).lower()))

	if doc.status == "Completed" and not (doc.resolution_note or "").strip():
		frappe.throw(_("Say what was done before completing the work order."))
	if doc.status == "Completed" and doc.work_type == "Statutory" \
			and not (doc.certificate_ref or "").strip():
		frappe.throw(_("A statutory job needs its certificate or report number before it "
		               "can be completed."))
	if doc.certificate_expiry and getdate(doc.certificate_expiry) < getdate(nowdate()) \
			and doc.status == "Completed":
		frappe.throw(_("The certificate entered has already expired."))

	_validate_room_out_of_order(doc, before)


def _validate_room_out_of_order(doc, before):
	flag_on = cint(doc.take_room_out_of_order)
	if not flag_on:
		return
	if not doc.room:
		frappe.throw(_("Pick the room to take out of order."))
	newly = not before or not cint(before.take_room_out_of_order) or before.room != doc.room
	if newly and doc.status in OPEN_STATES and not _can_take_room_out():
		frappe.throw(_("Only a maintenance or housekeeping supervisor, the front desk or an "
		               "admin can take a room out of order."), frappe.PermissionError)
	if doc.status not in OPEN_STATES:
		return
	room = frappe.db.get_value("Room", doc.room, ["housekeeping_status", "occupancy_status"],
	                           as_dict=True)
	if room and room.housekeeping_status != "Out of Order" and room.occupancy_status == "Occupied":
		frappe.throw(_("Room {0} is occupied. Move the guest before taking it out of order, "
		               "or clear 'Take room out of order' to log the job without closing "
		               "the room.").format(_room_no(doc.room)))


def before_insert_work_order(doc):
	if doc.work_type == "Emergency":
		doc.priority = "Urgent"
	if not doc.due_by:
		doc.due_by = add_to_date(now_datetime(), hours=sla_hours(doc.property, doc.priority))


def after_save_work_order(doc):
	"""on_update / after_insert: stamp the clock, then move room, asset and ticket."""
	before = doc.get_doc_before_save()
	old_status = before.status if before else None
	now = now_datetime()

	if doc.status != old_status:
		if doc.status == "In Progress" and not doc.started_on:
			doc.db_set("started_on", now, update_modified=False)
		if doc.status == "Completed":
			doc.db_set({"completed_on": now, "completed_by": frappe.session.user,
			            "breached": 1 if doc.due_by and now > get_datetime(doc.due_by) else 0},
			           update_modified=False)
			_log("complete_work_order", doc, f"{doc.category}: {doc.subject}")
		if old_status in DONE_STATES and doc.status in OPEN_STATES:
			# reopened: the finish stamps no longer describe it
			doc.db_set({"completed_on": None, "completed_by": None, "breached": 0},
			           update_modified=False)

	sync_room(doc, before)
	sync_asset(doc, before)
	if doc.status == "Completed" and old_status != "Completed":
		resolve_ticket(doc)
		if doc.schedule:
			upd = {"last_work_order": doc.name}
			# a certificate sets when the next statutory visit is due
			if doc.work_type == "Statutory" and doc.certificate_expiry:
				upd["next_due_date"] = getdate(doc.certificate_expiry)
			frappe.db.set_value("Maintenance Schedule", doc.schedule, upd, update_modified=False)


def on_trash_work_order(doc):
	if cint(doc.room_taken_out) and doc.room:
		_give_room_back(doc, exclude=doc.name)
	if doc.asset:
		_asset_back_in_service(doc.asset, exclude=doc.name)


# --- what moves with a work order -------------------------------------------

def _room_no(room: str | None) -> str:
	if not room:
		return ""
	return frappe.db.get_value("Room", room, "room_number") or room


def sync_room(doc, before=None):
	"""Out of order while open and flagged; back to Dirty when the last
	flagged order on the room closes."""
	wants_out = cint(doc.take_room_out_of_order) and doc.room and doc.status in OPEN_STATES
	old_room = before.room if before else None
	if before and cint(before.room_taken_out) and old_room and old_room != doc.room:
		# moved to another room: the old one comes back
		_give_room_back(doc, room=old_room, exclude=doc.name)
		doc.db_set("room_taken_out", 0, update_modified=False)

	if wants_out:
		status = frappe.db.get_value("Room", doc.room, "housekeeping_status")
		if status != "Out of Order":
			frappe.db.set_value("Room", doc.room, "housekeeping_status", "Out of Order")
			_log("room_status", doc, f"{status or '-'} → Out of Order ({doc.name})")
			from kamra.notifications import room_out_of_order
			room_out_of_order(doc.property, doc.room)
		if not cint(doc.room_taken_out):
			doc.db_set("room_taken_out", 1, update_modified=False)
	elif cint(doc.room_taken_out) and doc.room:
		_give_room_back(doc, exclude=doc.name)
		doc.db_set("room_taken_out", 0, update_modified=False)


def _give_room_back(doc, room: str | None = None, exclude: str | None = None):
	room = room or doc.room
	if not room or _open_ooo_orders(room, exclude=exclude):
		return
	if frappe.db.get_value("Room", room, "housekeeping_status") != "Out of Order":
		return  # someone already put it back by hand
	frappe.db.set_value("Room", room, "housekeeping_status", "Dirty")
	_log("room_status", doc, f"Out of Order → Dirty ({doc.name} {doc.status.lower()})")
	from kamra.notifications import record
	record(doc.property, "housekeeping",
	       f"Room {_room_no(room)} back from maintenance - needs a clean",
	       doctype="Room", name=room, route="/room-board")


def sync_asset(doc, before=None):
	if before and before.asset and before.asset != doc.asset:
		_asset_back_in_service(before.asset, exclude=doc.name)
	if not doc.asset:
		return
	status = frappe.db.get_value("Maintenance Asset", doc.asset, "status")
	if doc.status in ("In Progress", "On Hold") and status == "In Service":
		frappe.db.set_value("Maintenance Asset", doc.asset, "status", "Under Repair",
		                    update_modified=False)
	elif doc.status in DONE_STATES:
		if doc.status == "Completed":
			frappe.db.set_value("Maintenance Asset", doc.asset, "last_serviced_on",
			                    getdate(doc.completed_on or now_datetime()), update_modified=False)
			if flt(doc.meter_reading) > 0:
				a = frappe.get_doc("Maintenance Asset", doc.asset)
				if a.meter_unit and flt(doc.meter_reading) >= flt(a.meter_reading):
					apply_asset_reading(a, reading=doc.meter_reading)
			if doc.work_type == "Condition-based":
				# fixed: the asset is back to good unless someone says otherwise
				frappe.db.set_value("Maintenance Asset", doc.asset, {
					"condition": "Good", "condition_checked_on": getdate(nowdate())},
					update_modified=False)
		_asset_back_in_service(doc.asset, exclude=doc.name)


def _asset_back_in_service(asset: str, exclude: str | None = None):
	if frappe.db.get_value("Maintenance Asset", asset, "status") != "Under Repair":
		return
	still = frappe.get_all(WO, filters={"asset": asset, "name": ("!=", exclude or ""),
	                                    "status": ("in", ["In Progress", "On Hold"])}, limit=1)
	if not still:
		frappe.db.set_value("Maintenance Asset", asset, "status", "In Service",
		                    update_modified=False)


def resolve_ticket(doc):
	"""The guest request behind this order is answered by it."""
	if not doc.service_ticket:
		return
	ticket = frappe.get_doc("Service Ticket", doc.service_ticket)
	if ticket.status not in ("Open", "In Progress"):
		return
	ticket.status = "Resolved"
	note = (doc.resolution_note or "").strip()
	ticket.resolution_note = f"{doc.name}: {note}" if note else doc.name
	ticket.flags.from_work_order = True
	ticket.save(ignore_permissions=True)


def work_order_for_ticket(ticket) -> str | None:
	"""Raise (once) the work order for a Maintenance guest request."""
	existing = frappe.db.get_value(WO, {"service_ticket": ticket.name}, "name")
	if existing:
		return existing
	wo = frappe.get_doc({
		"doctype": WO,
		"property": ticket.property,
		"subject": ticket.subject,
		"work_type": "Corrective",
		"priority": ticket.priority or "Medium",
		"status": "Open",
		"room": ticket.room,
		"description": "\n".join(x for x in (
			ticket.description,
			_("Guest: {0}").format(ticket.guest_name) if ticket.guest_name else None,
		) if x),
		"service_ticket": ticket.name,
	})
	wo.insert(ignore_permissions=True)
	return wo.name


# --- preventive schedules ----------------------------------------------------

def validate_schedule(doc):
	for link, dt in (("asset", "Maintenance Asset"), ("room", "Room")):
		val = doc.get(link)
		if val and frappe.db.get_value(dt, val, "property") != doc.property:
			frappe.throw(_("{0} {1} is not in {2}.").format(_(dt), val, doc.property))
	if doc.asset and not doc.room:
		doc.room = frappe.db.get_value("Maintenance Asset", doc.asset, "room")
	if cint(doc.every) < 1:
		doc.every = 1
	if cint(doc.lead_days) < 0:
		doc.lead_days = 0
	if doc.work_type not in PLANNED_TYPES:
		frappe.throw(_("A schedule plans {0} work. Faults, emergencies and projects are raised "
		               "by hand.").format(", ".join(_(t) for t in PLANNED_TYPES)))
	if doc.trigger == METER:
		if not doc.asset:
			frappe.throw(_("A meter-reading schedule needs the asset whose meter it follows."))
		unit, reading = frappe.db.get_value("Maintenance Asset", doc.asset,
		                                    ["meter_unit", "meter_reading"])
		if not unit:
			frappe.throw(_("Asset {0} has no meter. Set its meter (run hours, cycles...) "
			               "first.").format(doc.asset))
		if flt(doc.meter_interval) <= 0:
			frappe.throw(_("Say how many {0} between services.").format(_(unit).lower()))
		if not flt(doc.next_meter_due):
			doc.next_meter_due = flt(reading) + flt(doc.meter_interval)
		if doc.work_type == "Preventive":
			doc.work_type = "Predictive"
	else:
		doc.trigger = "Calendar"
		if not doc.next_due_date:
			frappe.throw(_("Set the date this schedule is next due."))
	if cint(doc.take_room_out_of_order) and not doc.room:
		frappe.throw(_("Pick the room to take out of order."))


def _has_open_order(schedule) -> bool:
	return bool(frappe.db.exists(WO, {"schedule": schedule.name,
	                                  "status": ("in", list(OPEN_STATES))}))


def raise_from_schedule(schedule, today=None) -> str | None:
	"""Raise a calendar schedule's next work order if it is due and none is
	open. Returns the new work order's name, or None."""
	today = getdate(today or nowdate())
	if not cint(schedule.active) or schedule.trigger == METER or not schedule.next_due_date:
		return None
	due = getdate(schedule.next_due_date)
	if add_days(due, -cint(schedule.lead_days)) > today:
		return None
	if _has_open_order(schedule):
		return None  # one open order per schedule; the next comes when it closes

	due_by = get_datetime(f"{due} 23:59:59")
	if due_by < now_datetime():
		due_by = add_to_date(now_datetime(), hours=sla_hours(schedule.property, schedule.priority))
	name = _order_from_schedule(schedule, due_by)

	# roll forward past today, so a schedule left idle for months raises one
	# order, not a backlog
	nxt = next_due(due, schedule.frequency, schedule.every)
	while nxt <= today:
		nxt = next_due(nxt, schedule.frequency, schedule.every)
	frappe.db.set_value("Maintenance Schedule", schedule.name, {
		"next_due_date": nxt, "last_generated_on": today, "last_work_order": name,
	}, update_modified=False)
	return name


def _order_from_schedule(schedule, due_by, note: str | None = None) -> str:
	# a scheduled job never closes a room with a guest in it: the order is
	# raised without it and says so
	take_out = cint(schedule.take_room_out_of_order) and bool(schedule.room)
	notes = [schedule.instructions] if schedule.instructions else []
	if take_out and frappe.db.get_value("Room", schedule.room, "occupancy_status") == "Occupied":
		take_out = False
		notes.append(_("The room was occupied when this order was raised. Take it out of "
		               "order once the guest has left."))
	wo = frappe.get_doc({
		"doctype": WO,
		"property": schedule.property,
		"subject": schedule.title,
		"work_type": schedule.work_type or "Preventive",
		"category": schedule.category,
		"priority": schedule.priority or "Medium",
		"status": "Open",
		"room": schedule.room,
		"asset": schedule.asset,
		"location": schedule.location,
		"assigned_to_user": schedule.assigned_to_user,
		"vendor": schedule.vendor,
		"take_room_out_of_order": 1 if take_out else 0,
		"description": "\n\n".join(notes) or None,
		"due_by": due_by,
		"schedule": schedule.name,
	})
	if note:
		wo.description = "\n\n".join(x for x in (wo.description, note) if x)
	wo.flags.ignore_permissions = True
	wo.insert(ignore_permissions=True)
	return wo.name


# --- meter readings and condition (predictive / condition-based) -------------

def apply_asset_reading(asset, reading=None, condition: str | None = None,
                        note: str | None = None, reset: bool = False) -> list[str]:
	"""Record a meter reading and/or condition on an asset and raise what it
	calls for: predictive orders for meter schedules it has crossed, and a
	condition-based order when the asset is Poor or Critical. Returns the
	work orders raised."""
	raised: list[str] = []
	updates: dict = {}
	if reading not in (None, ""):
		reading = flt(reading)
		if not asset.meter_unit:
			frappe.throw(_("Asset {0} has no meter.").format(asset.asset_name))
		if reading < flt(asset.meter_reading) and not reset:
			frappe.throw(_("The reading {0} is below the last one ({1}). Tick 'meter reset' if "
			               "the meter was replaced.").format(reading, flt(asset.meter_reading)))
		updates.update({"meter_reading": reading, "meter_read_on": now_datetime()})
	if condition:
		if condition not in ("Good", "Fair", "Poor", "Critical"):
			frappe.throw(_("Condition must be Good, Fair, Poor or Critical."))
		updates.update({"condition": condition, "condition_checked_on": getdate(nowdate())})
	if not updates:
		frappe.throw(_("Enter a meter reading or a condition."))
	frappe.db.set_value("Maintenance Asset", asset.name, updates, update_modified=False)

	if reading not in (None, ""):
		for s in frappe.get_all("Maintenance Schedule",
		                        filters={"asset": asset.name, "trigger": METER, "active": 1},
		                        pluck="name"):
			sched = frappe.get_doc("Maintenance Schedule", s)
			if reset and flt(sched.next_meter_due) - flt(sched.meter_interval) > reading:
				# a new meter starts again from its own zero
				frappe.db.set_value("Maintenance Schedule", s, "next_meter_due",
				                    reading + flt(sched.meter_interval), update_modified=False)
				continue
			if flt(sched.next_meter_due) > reading or _has_open_order(sched):
				continue
			unit = _(asset.meter_unit).lower()
			name = _order_from_schedule(
				sched, add_to_date(now_datetime(), hours=sla_hours(sched.property, sched.priority)),
				note=_("Due at {0} {1}; meter reads {2}.").format(
					flt(sched.next_meter_due), unit, reading))
			nxt = flt(sched.next_meter_due)
			while nxt <= reading:
				nxt += flt(sched.meter_interval)
			frappe.db.set_value("Maintenance Schedule", s, {
				"next_meter_due": nxt, "last_generated_on": getdate(nowdate()),
				"last_work_order": name}, update_modified=False)
			raised.append(name)

	if condition in BAD_CONDITION and not frappe.db.exists(WO, {
			"asset": asset.name, "work_type": "Condition-based",
			"status": ("in", list(OPEN_STATES))}):
		wo = frappe.get_doc({
			"doctype": WO, "property": asset.property,
			"subject": _("{0} found in {1} condition").format(asset.asset_name,
			                                                    _(condition).lower()),
			"work_type": "Condition-based", "category": asset.category or "Other",
			"priority": BAD_CONDITION[condition], "status": "Open",
			"asset": asset.name, "room": asset.room, "location": asset.location,
			"vendor": asset.vendor, "description": note,
		})
		wo.insert(ignore_permissions=True)
		raised.append(wo.name)
	return raised


def generate_preventive_work_orders():
	"""Scheduler (daily): raise the work orders that are due, per property
	that runs Maintenance. Best effort per schedule - one bad row never
	stops the rest."""
	today = getdate(nowdate())
	for s in frappe.get_all("Maintenance Schedule",
	                        filters={"active": 1, "trigger": ("!=", METER),
	                                 "next_due_date": ("<=", add_days(today, 366))},
	                        fields=["name", "property"]):
		if not module_on(s.property):
			continue
		try:
			raise_from_schedule(frappe.get_doc("Maintenance Schedule", s.name), today)
			frappe.db.commit()
		except Exception:
			frappe.db.rollback()
			frappe.log_error(title=f"Preventive work order not raised: {s.name}")


# --- API ---------------------------------------------------------------------

_LIST_FIELDS = ["name", "subject", "work_type", "certificate_ref", "certificate_expiry", "category", "priority", "status", "room",
                "asset", "location", "assigned_to_user", "vendor", "due_by", "breached",
                "take_room_out_of_order", "total_cost", "service_ticket", "schedule",
                "started_on", "completed_on", "creation"]


@frappe.whitelist()
@require_roles(*REPORT_ROLES)
def maintenance_summary(property: str) -> dict:
	"""The numbers on top of the work order board."""
	assert_property_access(property)
	now = now_datetime()
	month_start = getdate(nowdate()).replace(day=1)
	row = frappe.db.sql(
		"""SELECT
		     SUM(status IN ('Open','In Progress','On Hold')) AS open_n,
		     SUM(status IN ('Open','In Progress','On Hold') AND due_by < %(now)s) AS overdue,
		     SUM(status IN ('Open','In Progress','On Hold')
		         AND (assigned_to_user IS NULL OR assigned_to_user = '')
		         AND (vendor IS NULL OR vendor = '')) AS unassigned,
		     SUM(status IN ('Open','In Progress','On Hold') AND assigned_to_user = %(me)s) AS mine,
		     SUM(status = 'Completed' AND completed_on >= %(ms)s) AS done_mtd,
		     SUM(CASE WHEN status = 'Completed' AND completed_on >= %(ms)s
		              THEN total_cost ELSE 0 END) AS cost_mtd
		   FROM `tabMaintenance Work Order` WHERE property = %(p)s""",
		{"p": property, "now": now, "me": frappe.session.user, "ms": month_start},
		as_dict=True)[0]
	ooo = frappe.db.count("Room", {"property": property, "housekeeping_status": "Out of Order"})
	due_pm = frappe.db.count("Maintenance Schedule", {
		"property": property, "active": 1, "next_due_date": ("<=", add_days(nowdate(), 7))})
	by_type = frappe.db.sql(
		"""SELECT work_type, COUNT(*) FROM `tabMaintenance Work Order`
		   WHERE property = %s AND status IN ('Open','In Progress','On Hold')
		   GROUP BY work_type""", property)
	return {
		"open_by_type": {t: n for t, n in by_type},
		"open": cint(row.open_n), "overdue": cint(row.overdue),
		"unassigned": cint(row.unassigned), "mine": cint(row.mine),
		"completed_this_month": cint(row.done_mtd), "cost_this_month": flt(row.cost_mtd),
		"rooms_out_of_order": ooo, "preventive_due_7_days": due_pm,
	}


@frappe.whitelist()
@require_roles(*REPORT_ROLES)
def work_orders_list(property: str, view: str = "open", room: str | None = None,
                     category: str | None = None, work_type: str | None = None,
                     limit: int = 100) -> list:
	"""Work orders for a property. view: open (default), mine, unassigned,
	overdue, completed, all. Open ones first, soonest due first."""
	assert_property_access(property)
	filters: dict = {"property": property}
	open_q = ("in", list(OPEN_STATES))
	if view in ("open", "mine", "unassigned", "overdue"):
		filters["status"] = open_q
	elif view == "completed":
		filters["status"] = "Completed"
	if view == "mine":
		filters["assigned_to_user"] = frappe.session.user
	elif view == "overdue":
		filters["due_by"] = ("<", now_datetime())
	if room:
		filters["room"] = frappe.db.get_value("Room", {"property": property, "room_number": room},
		                                      "name") or room
	if category:
		filters["category"] = category
	if work_type:
		filters["work_type"] = work_type
	rows = frappe.get_all(WO, filters=filters, fields=_LIST_FIELDS,
	                      order_by="due_by asc, creation desc",
	                      limit=min(cint(limit) or 100, 500))
	# work in hand first, then waiting, then on hold, then the finished
	rank = {"In Progress": 0, "Open": 1, "On Hold": 2}
	rows.sort(key=lambda r: rank.get(r.status, 3))
	if view == "unassigned":
		rows = [r for r in rows if not r.assigned_to_user and not r.vendor]
	now = now_datetime()
	for r in rows:
		r["room_number"] = _room_no(r.room) if r.room else None
		r["overdue"] = bool(r.status in OPEN_STATES and r.due_by and get_datetime(r.due_by) < now)
	return rows


@frappe.whitelist(methods=["POST"])
@require_roles(*REPORT_ROLES)
def create_work_order(property: str, subject: str, category: str | None = None,
                      priority: str | None = None, room: str | None = None,
                      asset: str | None = None, location: str | None = None,
                      description: str | None = None, take_room_out_of_order=0,
                      assigned_to_user: str | None = None,
                      work_type: str | None = None) -> dict:
	"""Report a fault. `room` takes the room number staff use (204) or its id."""
	assert_property_access(property)
	frappe.has_permission(WO, "create", throw=True)
	if room and not frappe.db.exists("Room", room):
		number = room
		room = frappe.db.get_value("Room", {"property": property, "room_number": number}, "name")
		if not room:
			frappe.throw(_("No room {0} in {1}.").format(number, property))
	doc = frappe.get_doc({
		"doctype": WO, "property": property, "subject": subject,
		"category": category or "Other", "priority": priority or "Medium",
		"work_type": work_type if work_type in WORK_TYPES else "Corrective",
		"room": room, "asset": asset, "location": location, "description": description,
		"take_room_out_of_order": cint(take_room_out_of_order),
		"assigned_to_user": assigned_to_user, "status": "Open",
	}).insert()
	return {"ok": True, "work_order": doc.name, "due_by": str(doc.due_by)}


@frappe.whitelist(methods=["POST"])
@require_roles(*MAINT_ROLES, "Front Desk", "Housekeeping Supervisor")
def set_work_order_status(work_order: str, status: str, note: str | None = None,
                          labour_hours=None, labour_cost=None, parts_cost=None,
                          vendor_cost=None, certificate_ref: str | None = None,
                          certificate_expiry: str | None = None, meter_reading=None) -> dict:
	"""Start, hold, complete, cancel or reopen a work order, with the note
	and costs that go with finishing it. DocPerm checked: taking away Write
	on the doctype takes the button away too."""
	allowed = (*OPEN_STATES, *DONE_STATES)
	if status not in allowed:
		frappe.throw(_("Invalid status. Use one of {0}").format(", ".join(allowed)))
	doc = frappe.get_doc(WO, work_order)
	assert_property_access(doc.property)
	frappe.has_permission(WO, "write", doc=doc, throw=True)
	if (note or "").strip():
		doc.resolution_note = note.strip()
	for field, val in (("labour_hours", labour_hours), ("labour_cost", labour_cost),
	                   ("parts_cost", parts_cost), ("vendor_cost", vendor_cost)):
		if val not in (None, ""):
			doc.set(field, flt(val))
	if (certificate_ref or "").strip():
		doc.certificate_ref = certificate_ref.strip()
	if certificate_expiry:
		doc.certificate_expiry = certificate_expiry
	if meter_reading not in (None, ""):
		doc.meter_reading = flt(meter_reading)
	doc.status = status
	doc.save()
	return {"ok": True, "work_order": doc.name, "status": doc.status}


@frappe.whitelist(methods=["POST"])
@require_roles(*MAINT_ROLES, "Front Desk", "Housekeeping Supervisor")
def assign_work_order(work_order: str, user: str | None = None,
                      vendor: str | None = None) -> dict:
	doc = frappe.get_doc(WO, work_order)
	assert_property_access(doc.property)
	frappe.has_permission(WO, "write", doc=doc, throw=True)
	if vendor and frappe.db.get_value("Vendor", vendor, "property") != doc.property:
		frappe.throw(_("Vendor {0} is not in {1}.").format(vendor, doc.property))
	doc.assigned_to_user = user or None
	doc.vendor = vendor or None
	doc.save()
	return {"ok": True, "work_order": doc.name}


@frappe.whitelist(methods=["POST"])
@require_roles(*REPORT_ROLES)
def work_order_from_ticket(ticket: str) -> dict:
	"""Raise the work order for a guest request by hand (any category)."""
	t = frappe.get_doc("Service Ticket", ticket)
	assert_property_access(t.property)
	frappe.has_permission(WO, "create", throw=True)
	return {"ok": True, "work_order": work_order_for_ticket(t)}


@frappe.whitelist(methods=["POST"])
@require_roles(*MANAGER_ROLES)
def run_schedule_now(schedule: str) -> dict:
	"""Raise the schedule's work order today, without waiting for its date.
	It counts as this cycle's order: the next one is a cycle from today."""
	s = frappe.get_doc("Maintenance Schedule", schedule)
	assert_property_access(s.property)
	if _has_open_order(s):
		frappe.throw(_("This schedule already has an open work order. Finish it first."))
	if s.trigger == METER:
		name = _order_from_schedule(
			s, add_to_date(now_datetime(), hours=sla_hours(s.property, s.priority)))
		reading = flt(frappe.db.get_value("Maintenance Asset", s.asset, "meter_reading"))
		frappe.db.set_value("Maintenance Schedule", s.name, {
			"next_meter_due": reading + flt(s.meter_interval),
			"last_generated_on": getdate(nowdate()), "last_work_order": name},
			update_modified=False)
		return {"ok": True, "work_order": name}
	s.lead_days = 0
	s.next_due_date = min(getdate(s.next_due_date or nowdate()), getdate(nowdate()))
	name = raise_from_schedule(s)
	return {"ok": True, "work_order": name}


@frappe.whitelist(methods=["POST"])
@require_roles(*MAINT_ROLES, "Housekeeping Supervisor")
def record_asset_reading(asset: str, reading=None, condition: str | None = None,
                         note: str | None = None, reset=0) -> dict:
	"""A technician's round: the meter reading and/or condition of one
	asset. Raises the predictive and condition-based work it calls for."""
	a = frappe.get_doc("Maintenance Asset", asset)
	assert_property_access(a.property)
	frappe.has_permission(WO, "create", throw=True)
	raised = apply_asset_reading(a, reading=reading, condition=condition, note=note,
	                             reset=bool(cint(reset)))
	return {"ok": True, "raised": raised}


@frappe.whitelist()
@require_roles(*REPORT_ROLES)
def asset_history(asset: str, limit: int = 50) -> dict:
	"""An asset's work orders, newest first, and what it has cost."""
	a = frappe.get_doc("Maintenance Asset", asset)
	assert_property_access(a.property)
	rows = frappe.get_all(WO, filters={"asset": asset}, fields=_LIST_FIELDS,
	                      order_by="creation desc", limit=min(cint(limit) or 50, 200))
	spent = frappe.db.sql(
		"""SELECT COALESCE(SUM(total_cost), 0) FROM `tabMaintenance Work Order`
		   WHERE asset = %s AND status = 'Completed'""", asset)[0][0]
	return {"work_orders": rows, "total_cost": flt(spent),
	        "open": sum(1 for r in rows if r.status in OPEN_STATES)}


@frappe.whitelist()
@require_roles(*REPORT_ROLES)
def room_work_orders(room: str, include_closed: int = 0) -> list:
	"""Work orders on one room - for the room drawer and history."""
	prop = frappe.db.get_value("Room", room, "property")
	if not prop:
		frappe.throw(_("Room {0} not found.").format(room), frappe.DoesNotExistError)
	assert_property_access(prop)
	filters = {"room": room}
	if not cint(include_closed):
		filters["status"] = ("in", list(OPEN_STATES))
	return frappe.get_all(WO, filters=filters, fields=_LIST_FIELDS,
	                      order_by="creation desc", limit=100)
