/* Maintenance drawer panels.
 *
 *  WorkOrderPanel  - move a work order along: start, hold, complete (with what
 *                    was done, costs and a certificate for statutory work),
 *                    cancel or reopen.
 *  AssetPanel      - a technician's round on one asset: meter reading and
 *                    condition, which raise predictive and condition-based
 *                    work; and the asset's work order history and cost.
 *  SchedulePanel   - raise the schedule's work order now. */

import { useCallback, useEffect, useState } from "react"
import { Link } from "react-router-dom"
import { call } from "../lib/api"
import { serverError, type Row } from "../lib/resource"
import { useT } from "../lib/i18n"
import { fmtMoney } from "../lib/money"
import { Button } from "./ui/button"

const inputCls =
  "w-full rounded-lg border border-zinc-300 bg-white px-3 py-1.5 text-sm " +
  "focus:outline-2 focus:outline-offset-1 focus:outline-brand-600"

const OPEN = ["Open", "In Progress", "On Hold"]

function Notice({ error, done }: { error: string | null; done: string | null }) {
  const { t } = useT()
  if (error)
    return (
      <p role="alert" className="rounded-lg border border-rose-200 bg-rose-50 px-3 py-2 text-sm text-rose-700">
        {t(error)}
      </p>
    )
  if (done)
    return (
      <p role="status" className="rounded-lg border border-green-200 bg-green-50 px-3 py-2 text-sm text-green-800">
        {done}
      </p>
    )
  return null
}

export function WorkOrderPanel({ row, reload }: { row: Row; reload: () => void }) {
  const { t } = useT()
  const status = String(row.status ?? "Open")
  const statutory = row.work_type === "Statutory"
  const [finishing, setFinishing] = useState(false)
  const [note, setNote] = useState(String(row.resolution_note ?? ""))
  const [cert, setCert] = useState(String(row.certificate_ref ?? ""))
  const [hours, setHours] = useState("")
  const [labour, setLabour] = useState("")
  const [parts, setParts] = useState("")
  const [vendor, setVendor] = useState("")
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  async function move(to: string, extra: Record<string, unknown> = {}) {
    setBusy(true)
    setError(null)
    try {
      await call("kamra.maintenance.set_work_order_status", { work_order: row.name, status: to, ...extra })
      setDone(t("Work order {status}.", { status: t(to).toLowerCase() }))
      setFinishing(false)
      reload()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  async function complete() {
    if (!note.trim()) return setError(t("Say what was done before completing the work order."))
    if (statutory && !cert.trim())
      return setError(t("A statutory job needs its certificate or report number before it can be completed."))
    await move("Completed", {
      note, labour_hours: hours, labour_cost: labour, parts_cost: parts, vendor_cost: vendor,
      certificate_ref: statutory ? cert.trim() : null,
    })
  }

  return (
    <div className="space-y-3">
      <h3 className="text-sm font-semibold text-zinc-700">{t("Progress")}</h3>
      <Notice error={error} done={done} />
      {row.service_ticket ? (
        <p className="text-xs text-zinc-500">
          {t("Raised from guest request {ticket}. Completing this order resolves it.", {
            ticket: String(row.service_ticket),
          })}
        </p>
      ) : null}
      {OPEN.includes(status) && !finishing && (
        <div className="flex flex-wrap gap-2">
          {status !== "In Progress" && (
            <Button disabled={busy} onClick={() => move("In Progress")}>{t("Start work")}</Button>
          )}
          {status === "In Progress" && (
            <Button variant="outline" disabled={busy} onClick={() => move("On Hold")}>{t("Put on hold")}</Button>
          )}
          <Button disabled={busy} onClick={() => setFinishing(true)}>{t("Complete")}</Button>
          <Button
            variant="ghost"
            disabled={busy}
            onClick={() => confirm(t("Cancel this work order?")) && move("Cancelled")}
          >
            {t("Cancel order")}
          </Button>
        </div>
      )}
      {finishing && (
        <div className="space-y-2 rounded-lg border border-zinc-200 p-3">
          <label className="block text-xs font-medium text-zinc-600">
            {t("What was done")}
            <textarea className={inputCls} rows={2} value={note} onChange={(e) => setNote(e.target.value)} />
          </label>
          {statutory && (
            <label className="block text-xs font-medium text-zinc-600">
              {t("Certificate / report no.")}
              <input className={inputCls} value={cert} onChange={(e) => setCert(e.target.value)} />
            </label>
          )}
          <div className="grid grid-cols-2 gap-2">
            <label className="block text-xs font-medium text-zinc-600">
              {t("Labour hours")}
              <input className={inputCls} inputMode="decimal" value={hours} onChange={(e) => setHours(e.target.value)} />
            </label>
            <label className="block text-xs font-medium text-zinc-600">
              {t("Labour cost")}
              <input className={inputCls} inputMode="decimal" value={labour} onChange={(e) => setLabour(e.target.value)} />
            </label>
            <label className="block text-xs font-medium text-zinc-600">
              {t("Parts cost")}
              <input className={inputCls} inputMode="decimal" value={parts} onChange={(e) => setParts(e.target.value)} />
            </label>
            <label className="block text-xs font-medium text-zinc-600">
              {t("Vendor cost")}
              <input className={inputCls} inputMode="decimal" value={vendor} onChange={(e) => setVendor(e.target.value)} />
            </label>
          </div>
          <div className="flex gap-2">
            <Button disabled={busy} onClick={complete}>{t("Complete work order")}</Button>
            <Button variant="ghost" disabled={busy} onClick={() => setFinishing(false)}>{t("Back")}</Button>
          </div>
        </div>
      )}
      {status === "Completed" || status === "Cancelled" ? (
        <Button variant="outline" disabled={busy} onClick={() => move("Open")}>{t("Reopen")}</Button>
      ) : null}
    </div>
  )
}

interface AssetHistory {
  work_orders: Row[]
  total_cost: number
  open: number
}

export function AssetPanel({ row, reload }: { row: Row; reload: () => void }) {
  const { t } = useT()
  const [hist, setHist] = useState<AssetHistory | null>(null)
  const [reading, setReading] = useState("")
  const [condition, setCondition] = useState("")
  const [note, setNote] = useState("")
  const [reset, setReset] = useState(false)
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)
  const metered = !!row.meter_unit

  const load = useCallback(() => {
    call<AssetHistory>("kamra.maintenance.asset_history", { asset: row.name })
      .then(setHist)
      .catch((e) => setError(serverError(e)))
  }, [row.name])
  useEffect(load, [load])

  async function record() {
    if (!reading && !condition) return setError(t("Enter a meter reading or a condition."))
    setBusy(true)
    setError(null)
    try {
      const r = await call<{ raised: string[] }>("kamra.maintenance.record_asset_reading", {
        asset: row.name,
        reading: reading || null,
        condition: condition || null,
        note: note || null,
        reset: reset ? 1 : 0,
      })
      setDone(
        r.raised.length
          ? t("Recorded. Work order raised: {orders}.", { orders: r.raised.join(", ") })
          : t("Recorded. Nothing is due."),
      )
      setReading("")
      setCondition("")
      setNote("")
      setReset(false)
      load()
      reload()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-4">
      <section className="space-y-2">
        <h3 className="text-sm font-semibold text-zinc-700">{t("Record a check")}</h3>
        <Notice error={error} done={done} />
        <div className="grid grid-cols-2 gap-2">
          {metered && (
            <label className="block text-xs font-medium text-zinc-600">
              {t("Meter reading ({unit})", { unit: t(String(row.meter_unit)) })}
              <input className={inputCls} inputMode="decimal" value={reading} onChange={(e) => setReading(e.target.value)} />
            </label>
          )}
          <label className="block text-xs font-medium text-zinc-600">
            {t("Condition")}
            <select className={inputCls} value={condition} onChange={(e) => setCondition(e.target.value)}>
              <option value="">{t("No change")}</option>
              {["Good", "Fair", "Poor", "Critical"].map((c) => (
                <option key={c} value={c}>{t(c)}</option>
              ))}
            </select>
          </label>
        </div>
        <label className="block text-xs font-medium text-zinc-600">
          {t("Note")}
          <input className={inputCls} value={note} onChange={(e) => setNote(e.target.value)} />
        </label>
        {metered && (
          <label className="flex items-center gap-2 text-xs text-zinc-600">
            <input type="checkbox" checked={reset} onChange={(e) => setReset(e.target.checked)} />
            {t("The meter was replaced or reset")}
          </label>
        )}
        <p className="text-xs text-zinc-500">
          {t("A reading past a meter schedule raises a predictive work order. Poor or Critical raises a condition-based one.")}
        </p>
        <Button disabled={busy} onClick={record}>{t("Record")}</Button>
      </section>

      <section className="space-y-2">
        <h3 className="text-sm font-semibold text-zinc-700">{t("Work order history")}</h3>
        {hist === null ? (
          <p className="text-sm text-zinc-400">{t("Loading…")}</p>
        ) : hist.work_orders.length === 0 ? (
          <p className="text-sm text-zinc-400">{t("No work orders on this asset yet.")}</p>
        ) : (
          <>
            <p className="text-xs text-zinc-500">
              {t("{open} open. Spent on completed work: {cost}.", {
                open: String(hist.open), cost: fmtMoney(hist.total_cost),
              })}
            </p>
            <ul className="divide-y divide-zinc-100 text-sm">
              {hist.work_orders.map((w) => (
                <li key={w.name} className="flex items-baseline justify-between gap-3 py-1.5">
                  <span>
                    <span className="font-medium text-zinc-800">{String(w.subject)}</span>{" "}
                    <span className="text-xs text-zinc-500">
                      {w.name}, {t(String(w.work_type))}
                    </span>
                  </span>
                  <span className="shrink-0 text-xs text-zinc-600">{t(String(w.status))}</span>
                </li>
              ))}
            </ul>
            <Link to="/maintenance" className="text-xs font-medium text-brand-700 hover:underline">
              {t("Open work orders")}
            </Link>
          </>
        )}
      </section>
    </div>
  )
}

export function SchedulePanel({ row, reload }: { row: Row; reload: () => void }) {
  const { t } = useT()
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [done, setDone] = useState<string | null>(null)

  async function raiseNow() {
    if (!confirm(t("Raise this schedule's work order now? It counts as this cycle's job."))) return
    setBusy(true)
    setError(null)
    try {
      const r = await call<{ work_order: string | null }>("kamra.maintenance.run_schedule_now", {
        schedule: row.name,
      })
      setDone(r.work_order ? t("Work order {name} raised.", { name: r.work_order }) : t("Nothing was raised."))
      reload()
    } catch (e) {
      setError(serverError(e))
    } finally {
      setBusy(false)
    }
  }

  return (
    <div className="space-y-2">
      <h3 className="text-sm font-semibold text-zinc-700">{t("Work orders")}</h3>
      <Notice error={error} done={done} />
      {row.last_work_order ? (
        <p className="text-xs text-zinc-500">
          {t("Last raised: {name}.", { name: String(row.last_work_order) })}
        </p>
      ) : null}
      <Button variant="outline" disabled={busy} onClick={raiseNow}>{t("Raise work order now")}</Button>
    </div>
  )
}
