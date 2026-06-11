import { useEffect, useMemo, useRef, useState } from 'react'
import { Link, useParams } from 'react-router-dom'
import { listRecords, type BatchSummary, type RecordRow } from '../api'
import { recordReasons } from '../plain'

type Filter = 'all' | 'open' | 'failed' | 'review' | 'passed'

const FILTERS: { key: Filter; label: string }[] = [
  { key: 'all', label: 'All' },
  { key: 'open', label: 'Open' },
  { key: 'failed', label: 'Failed' },
  { key: 'review', label: 'Needs review' },
  { key: 'passed', label: 'Passed' },
]

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
    const filtered = all.filter((r) => {
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
    })
    return filtered.sort((a, b) => rank(a) - rank(b) || a.filename.localeCompare(b.filename))
  }, [records, filter])

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
              <>{' · '}<span className="font-medium text-amber-700">{summary.needs_review} need review</span></>
            )}
            {summary.errors > 0 && <>{' · '}{summary.errors} couldn’t process</>}
            {' · '}<b>{summary.open}</b> open
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

function StatusPill({ record }: { record: RecordRow }) {
  if (record.state === 'error')
    return <span className="whitespace-nowrap rounded-full bg-stone-200 px-2.5 py-0.5 text-xs font-medium text-stone-700">Couldn’t process</span>
  switch (record.auto_status) {
    case 'Fail':
      return <span className="whitespace-nowrap rounded-full bg-red-100 px-2.5 py-0.5 text-xs font-medium text-red-800">Fail</span>
    case 'Needs Review':
      return <span className="whitespace-nowrap rounded-full bg-amber-100 px-2.5 py-0.5 text-xs font-medium text-amber-800">Needs review</span>
    case 'Pass':
      return <span className="whitespace-nowrap rounded-full bg-green-100 px-2.5 py-0.5 text-xs font-medium text-green-800">Pass</span>
    default:
      return <span className="whitespace-nowrap rounded-full bg-stone-100 px-2.5 py-0.5 text-xs text-stone-500">Working…</span>
  }
}

function DispositionTag({ record }: { record: RecordRow }) {
  if (record.state !== 'done') return null
  if (record.disposition === null)
    return <span className="whitespace-nowrap text-xs font-medium text-stone-500">awaiting your call</span>
  const label =
    record.dispositioned_by === 'system'
      ? `${record.disposition} automatically`
      : `${record.disposition} by ${record.dispositioned_by}`
  return <span className="whitespace-nowrap text-xs text-stone-500">{label}</span>
}

function RecordRowItem({ record: r }: { record: RecordRow }) {
  const quiet = r.auto_status === 'Pass' && r.state === 'done'
  const reasons = quiet ? [] : recordReasons(r)
  const title = r.form?.brand_name || r.ttb_id || r.filename

  if (r.state === 'pending' || r.state === 'processing') {
    return (
      <li className="flex items-center justify-between rounded-xl border border-stone-200 bg-white/60 px-4 py-2.5 text-stone-400">
        <span className="text-sm">{r.filename}</span>
        <StatusPill record={r} />
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
            <DispositionTag record={r} />
            <StatusPill record={r} />
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
            <DispositionTag record={r} />
            <StatusPill record={r} />
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
