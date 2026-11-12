/* The kinds of maintenance Kamra knows (kamra/maintenance.py WORK_TYPES).
 *  Faults, emergencies, condition-based work and projects are raised by hand
 *  or by an asset check; the planned kinds come from schedules. */

export const MAINTENANCE_CATEGORIES = [
  "HVAC", "Plumbing", "Electrical", "Elevator", "Kitchen Equipment", "Laundry Equipment",
  "Pool & Spa", "Fire & Safety", "IT & AV", "Furniture & Fixtures", "Building", "Grounds", "Other",
]
export const WORK_TYPES = [
  "Corrective", "Emergency", "Preventive", "Predictive", "Condition-based", "Routine",
  "Inspection", "Statutory", "Guest Room PM", "Seasonal", "Improvement",
]
export const PLANNED_TYPES = [
  "Preventive", "Predictive", "Routine", "Inspection", "Statutory", "Guest Room PM", "Seasonal",
]
