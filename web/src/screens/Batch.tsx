import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { listRecords, type BatchSummary, type RecordRow } from '../api'
import { recordReasons } from '../plain'

type Filter = 'all' | 'open' | 'failed' | 'review' | 'passed'

// Two axes, two filter families: "To review" is workflow state (no
// decision yet); Flagged/Failed/Passed are what the checks concluded,
// regardless of any decision since.
const FILTERS: { key: Filter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'open', label: 'To review' },
  { key: 'review', label: 'Flagged' },
  { key: 'failed', label: 'Failed' },
  { key: 'passed', label: 'Passed' },
]

function matchesFilter(r: RecordRow, filter: Filter): boolean {
  switch (filter) {
    case 'open':
      return r.state === 'done' && r.disposition === null
    case 'failed':
      return r.auto_status === 'Fail' || r.state === 'error'
    case 'review':
      return r.auto_status === 'Needs Review'
    case 'passed':
      return r.auto_status === 'Pass'
    default:
      return true
  }
}

const STATUS_RANK: Record<string, number> = {
  Fail: 0,
  error: 1,
  'Needs Review': 2,
  Pass: 3,
}

function rank(r: RecordRow): number {
  if (r.state === 'error') return STATUS_RANK.error
  return STATUS_RANK[r.auto_status ?? ''] ?? 4
}

function openness(r: RecordRow): number {
  return r.state === 'done' && r.disposition === null ? 0 : 1
}

export default function Batch() {
  const { batchId } = useParams()
  const [records, setRecords] = useState<Map<string, RecordRow>>(new Map())
  const [summary, setSummary] = useState<BatchSummary | null>(null)
  const [filter, setFilter] = useState<Filter>('all')
  const esRef = useRef<EventSource | null>(null)

  useEffect(() => {
    if (!batchId) return
    listRecords(batchId).then((rows) => {
      setRecords(new Map(rows.map((r) => [r.id, r])))
    })
    const es = new EventSource(`/api/batches/${batchId}/events`)
    esRef.current = es
    es.addEventListener('record', (e) => {
      const r: RecordRow = JSON.parse((e as MessageEvent).data)
      setRecords((prev) => new Map(prev).set(r.id, r))
    })
    es.addEventListener('summary', (e) => {
      setSummary(JSON.parse((e as MessageEvent).data))
    })
    es.addEventListener('done', () => es.close())
    es.onerror = () => es.close()
    return () => es.close()
  }, [batchId])

  const rows = useMemo(() => {
    const all = Array.from(records.values())
    return all
      .filter((r) => matchesFilter(r, filter))
      .sort(
        (a, b) =>
          openness(a) - openness(b) ||
          rank(a) - rank(b) ||
          a.filename.localeCompare(b.filename),
      )
  }, [records, filter])

  const counts = useMemo(() => {
    const all = Array.from(records.values())
    return Object.fromEntries(
      FILTERS.map((f) => [f.key, all.filter((r) => matchesFilter(r, f.key)).length]),
    ) as Record<Filter, number>
  }, [records])

  const processing = summary ? summary.total - summary.processed : 0

  return (
    <div className="mx-auto min-h-screen max-w-4xl px-6 py-8">
      <header className="mb-6 flex flex-wrap items-baseline justify-between gap-2">
        <div>
          <Link to="/" className="text-sm text-blue-700 hover:underline">
            ← Upload
          </Link>
          <h1 className="mt-1 text-xl font-semibold tracking-tight">Batch review</h1>
        </div>
        {summary && (
          <p className="text-sm text-stone-600">
            <b>{summary.processed}</b> processed
            {processing > 0 && <>{' · '}{processing} still working</>}
            {summary.failed > 0 && (
              <>{' · '}<span className="font-medium text-red-700">{summary.failed} failed</span></>
            )}
            {summary.needs_review > 0 && (
              <>{' · '}<span className="font-medium text-amber-700">{summary.needs_review} flagged</span></>
            )}
            {summary.errors > 0 && <>{' · '}{summary.errors} couldn’t process</>}
            {' · '}<b>{summary.open}</b> to review
          </p>
        )}
      </header>

      {summary && processing > 0 && (
        <div className="mb-6 h-1.5 overflow-hidden rounded-full bg-stone-200">
          <div
            className="h-full rounded-full bg-blue-500 transition-all duration-500"
            style={{ width: `${(summary.processed / Math.max(summary.total, 1)) * 100}%` }}
          />
        </div>
      )}

      <div className="mb-4 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap items-center gap-2">
          {FILTERS.map((f) => (
            <button
              key={f.key}
              onClick={() => setFilter(f.key)}
              className={`whitespace-nowrap rounded-full px-3 py-1 text-sm transition-colors ${
                filter === f.key
                  ? 'bg-stone-900 text-white'
                  : 'bg-white text-stone-700 ring-1 ring-stone-300 hover:bg-stone-50'
              }`}
            >
              {f.label}
              {f.key !== 'all' && <span className="opacity-60"> {counts[f.key]}</span>}
            </button>
          ))}
        </div>
        <div className="flex flex-wrap items-center gap-2">
          <a
            href={`/api/batches/${batchId}/export.csv?scope=${filter}`}
            className="whitespace-nowrap rounded-lg bg-white px-3 py-1 text-sm text-stone-700 ring-1 ring-stone-300 hover:bg-stone-50"
          >
            Export CSV
          </a>
          <a
            href={`/api/batches/${batchId}/export.pdf?scope=${filter}`}
            className="whitespace-nowrap rounded-lg bg-white px-3 py-1 text-sm text-stone-700 ring-1 ring-stone-300 hover:bg-stone-50"
          >
            Export PDF
          </a>
          <button
            onClick={async () => {
              if (
                window.confirm(
                  'Delete this batch and purge the uploaded PDFs and label images? Export first — the export is what you keep.',
                )
              ) {
                await fetch(`/api/batches/${batchId}`, { method: 'DELETE' })
                window.location.href = '/'
              }
            }}
            className="whitespace-nowrap rounded-lg bg-white px-3 py-1 text-sm text-red-700 ring-1 ring-red-200 hover:bg-red-50"
          >
            Delete
          </button>
        </div>
      </div>

      <ul className="space-y-2">
        {rows.map((r) => (
          <RecordRowItem key={r.id} record={r} />
        ))}
        {rows.length === 0 && (
          <li className="rounded-xl border border-stone-200 bg-white p-6 text-center text-stone-500">
            {records.size === 0 ? 'Records will appear here as they finish.' : 'Nothing matches this filter.'}
          </li>
        )}
      </ul>
    </div>
  )
}

const CHIP = 'whitespace-nowrap rounded-full px-2.5 py-0.5 text-xs font-medium'

function VerdictPill({ status }: { status: string | null }) {
  switch (status) {
    case 'Fail':
      return <span className={`${CHIP} bg-red-100 text-red-800`}>Fail</span>
    case 'Needs Review':
      return <span className={`${CHIP} bg-amber-100 text-amber-800`}>Needs review</span>
    case 'Pass':
      return <span className={`${CHIP} bg-green-100 text-green-800`}>Pass</span>
    default:
      return <span className={`${CHIP} bg-stone-100 font-normal text-stone-500`}>Working…</span>
  }
}

// The decision and the checks disagree — that's the audit-worthy case,
// the only time the checks' verdict is repeated on a decided row.
function checksDisagreement(r: RecordRow): string | null {
  if (r.disposition === 'Approved' && r.auto_status === 'Fail')
    return 'checks recommended fail'
  if (r.disposition === 'Approved' && r.auto_status === 'Needs Review')
    return 'checks flagged this'
  if (r.disposition === 'Rejected' && r.auto_status === 'Pass')
    return 'checks passed this'
  return null
}

function RowStatus({ record: r }: { record: RecordRow }) {
  if (r.state === 'error')
    return <span className={`${CHIP} bg-stone-200 text-stone-700`}>Couldn’t process</span>
  if (r.state === 'vision_queued')
    return (
      <span className={`${CHIP} bg-stone-100 font-normal text-stone-500`}>Waiting…</span>
    )
  if (r.state === 'vision_reading')
    return (
      <span className={`${CHIP} bg-stone-100 font-normal text-stone-500`}>
        Re-reading with AI…
      </span>
    )
  if (r.state !== 'done') return <VerdictPill status={null} />
  if (r.disposition === null) {
    // Open: amber/red here always means "work to do".
    return (
      <>
        <span className="whitespace-nowrap text-xs font-medium text-stone-500">
          awaiting your call
        </span>
        <VerdictPill status={r.auto_status} />
      </>
    )
  }
  const disagreement = checksDisagreement(r)
  const by = r.dispositioned_by === 'system' ? 'automatically' : `by ${r.dispositioned_by}`
  return (
    <>
      {disagreement && (
        <span className="whitespace-nowrap text-xs font-medium text-amber-700">
          {disagreement}
        </span>
      )}
      <span className="whitespace-nowrap text-xs text-stone-500">{by}</span>
      <span
        className={`${CHIP} ${
          r.disposition === 'Approved' ? 'bg-green-100 text-green-800' : 'bg-red-100 text-red-800'
        }`}
      >
        {r.disposition}
      </span>
    </>
  )
}

function RecordRowItem({ record: r }: { record: RecordRow }) {
  const quiet = r.auto_status === 'Pass' && r.state === 'done'
  const reasons = quiet ? [] : recordReasons(r)
  const title = r.form?.brand_name || r.ttb_id || r.filename

  if (r.state !== 'done' && r.state !== 'error') {
    return (
      <li className="flex items-center justify-between rounded-xl border border-stone-200 bg-white/60 px-4 py-2.5 text-stone-400">
        <span className="text-sm">{r.filename}</span>
        <RowStatus record={r} />
      </li>
    )
  }

  if (quiet) {
    return (
      <li>
        <Link
          to={`/records/${r.id}`}
          className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1 rounded-xl border border-green-200/70 bg-green-50/50 px-4 py-2.5 hover:bg-green-50"
        >
          <span className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5 text-sm">
            <span className="font-medium text-stone-800">{title}</span>
            <span className="text-stone-500">{r.ttb_id}</span>
          </span>
          <span className="flex shrink-0 items-center gap-3">
            <RowStatus record={r} />
          </span>
        </Link>
      </li>
    )
  }

  return (
    <li>
      <Link
        to={r.state === 'error' ? '#' : `/records/${r.id}`}
        className="block rounded-xl border border-stone-200 bg-white px-4 py-3 shadow-sm transition-shadow hover:shadow"
      >
        <div className="flex flex-wrap items-center justify-between gap-x-3 gap-y-1">
          <span className="flex flex-wrap items-baseline gap-x-3 gap-y-0.5">
            <span className="font-medium">{title}</span>
            <span className="text-sm text-stone-500">{r.ttb_id}</span>
            {r.form?.class_type_description && (
              <span className="text-sm text-stone-500">{r.form.class_type_description}</span>
            )}
          </span>
          <span className="flex shrink-0 items-center gap-3">
            <RowStatus record={r} />
          </span>
        </div>
        {reasons.length > 0 && (
          <ul className="mt-2 space-y-0.5">
            {reasons.map((reason, i) => (
              <li key={i} className="text-sm text-stone-600">
                {reason}
              </li>
            ))}
          </ul>
        )}
      </Link>
    </li>
  )
}
