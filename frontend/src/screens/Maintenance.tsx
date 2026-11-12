/* Maintenance - the work order board.
 *
 *  A strip of what needs attention (overdue, unassigned, rooms out of order,
 *  planned jobs due this week, what is open by type) above the list of work
 *  orders. Each tile filters nothing by itself; it tells the engineer where
 *  to look, and the list's own filters do the narrowing. */

import { useCallback, useEffect, useState } from "react"
import { call, getCurrentProperty } from "../lib/api"
import { serverError } from "../lib/resource"
import { useT } from "../lib/i18n"
import { fmtMoney } from "../lib/money"
import { ResourceScreen } from "../components/ResourceScreen"
import { workOrdersConfig } from "./configs"
import { WORK_TYPES } from "../lib/maintenance"

interface Summary {
  open: number
  overdue: number
  unassigned: number
  mine: number
  completed_this_month: number
  cost_this_month: number
  rooms_out_of_order: number
  preventive_due_7_days: number
  open_by_type: Record<string, number>
}

function Tile({ label, value, alert }: { label: string; value: string | number; alert?: boolean }) {
  return (
    <div className="rounded-lg border border-zinc-200 bg-white px-3 py-2">
      <p className="text-xs text-zinc-500">{label}</p>
      <p className={alert ? "text-xl font-semibold text-rose-700" : "text-xl font-semibold text-zinc-900"}>
        {value}
      </p>
    </div>
  )
}

export default function Maintenance() {
  const { t } = useT()
  const [s, setS] = useState<Summary | null>(null)
  const [error, setError] = useState<string | null>(null)

  const load = useCallback(() => {
    call<Summary>("kamra.maintenance.maintenance_summary", { property: getCurrentProperty() })
      .then((r) => {
        setS(r)
        setError(null)
      })
      .catch((e) => setError(serverError(e)))
  }, [])
  // the shell remounts the page when the property changes
  useEffect(load, [load])

  const types = s ? WORK_TYPES.filter((k) => s.open_by_type?.[k]) : []

  return (
    <div className="space-y-4">
      {error && (
        <p role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
          {t(error)}
        </p>
      )}
      {s && (
        <section aria-label={t("Maintenance at a glance")} className="space-y-2">
          <div className="grid grid-cols-2 gap-2 sm:grid-cols-3 lg:grid-cols-6">
            <Tile label={t("Open")} value={s.open} />
            <Tile label={t("Overdue")} value={s.overdue} alert={s.overdue > 0} />
            <Tile label={t("Unassigned")} value={s.unassigned} alert={s.unassigned > 0} />
            <Tile label={t("Rooms out of order")} value={s.rooms_out_of_order} />
            <Tile label={t("Planned, due in 7 days")} value={s.preventive_due_7_days} />
            <Tile label={t("Spent this month")} value={fmtMoney(s.cost_this_month)} />
          </div>
          {types.length > 0 && (
            <p className="text-xs text-zinc-600">
              {t("Open by type:")}{" "}
              {types.map((k, i) => (
                <span key={k}>
                  {i > 0 && ", "}
                  <span className={k === "Emergency" ? "font-semibold text-rose-700" : undefined}>
                    {t(k)} {s.open_by_type[k]}
                  </span>
                </span>
              ))}
            </p>
          )}
        </section>
      )}
      <ResourceScreen config={workOrdersConfig} />
    </div>
  )
}
