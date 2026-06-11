import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  cropUrl,
  getRecord,
  listRecords,
  pdfUrl,
  setDisposition,
  type RecordRow,
  type Verdict,
} from '../api'
import { agentName, fieldLabel, setAgentName, verdictSentence, warningHeadline } from '../plain'

export default function Detail() {
  const { recordId } = useParams()
  const navigate = useNavigate()
  const [record, setRecord] = useState<RecordRow | null>(null)
  const [siblings, setSiblings] = useState<RecordRow[]>([])
  const [note, setNote] = useState('')
  const [agent, setAgent] = useState(agentName())
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  useEffect(() => {
    if (!recordId) return
    setNote('')
    setError(null)
    getRecord(recordId).then((r) => {
      setRecord(r)
      setNote(r.note ?? '')
      listRecords(r.batch_id).then(setSiblings)
    })
  }, [recordId])

  const nextOpen = useMemo(() => {
    if (!record) return null
    const rank = (s: string | null) => (s === 'Fail' ? 0 : s === 'Needs Review' ? 1 : 2)
    const open = siblings
      .filter((s) => s.state === 'done' && s.disposition === null && s.id !== record.id)
      .sort((a, b) => rank(a.auto_status) - rank(b.auto_status))
    return open[0] ?? null
  }, [siblings, record])

  async function act(disposition: 'Approved' | 'Rejected') {
    if (!record) return
    if (!agent.trim()) {
      setError('Add your name or initials first, so the record shows who made the call.')
      return
    }
    setAgentName(agent.trim())
    setSaving(true)
    setError(null)
    try {
      await setDisposition(record.id, disposition, agent.trim(), note.trim() || undefined)
      if (nextOpen) navigate(`/records/${nextOpen.id}`)
      else navigate(`/batches/${record.batch_id}`)
    } catch (e) {
      setSaving(false)
      setError(`Couldn't save that. ${e instanceof Error ? e.message : ''}`)
    }
  }

  if (!record) return <div className="p-10 text-stone-500">Loading…</div>

  const form = record.form

  return (
    <div className="mx-auto min-h-screen max-w-6xl px-6 py-8">
      <header className="mb-6 flex items-center justify-between">
        <div>
          <Link to={`/batches/${record.batch_id}`} className="text-sm text-blue-700 hover:underline">
            ← Back to batch
          </Link>
          <h1 className="mt-1 text-xl font-semibold tracking-tight">
            {form?.brand_name ?? record.filename}
            <span className="ml-3 text-base font-normal text-stone-500">{record.ttb_id}</span>
          </h1>
          <p className="mt-0.5 text-sm text-stone-500">
            {[form?.fanciful_name, form?.class_type_description, form?.product_type, form?.source]
              .filter(Boolean)
              .join(' · ')}
          </p>
        </div>
        <div className="flex items-start gap-4">
          <a
            href={`/api/records/${record.id}/export.pdf`}
            className="rounded-lg bg-white px-3 py-1.5 text-sm text-stone-700 ring-1 ring-stone-300 hover:bg-stone-50"
          >
            Record PDF
          </a>
          <AutoStatusBadge record={record} />
        </div>
      </header>

      <div className="grid grid-cols-1 gap-6 lg:grid-cols-2">
        {/* Left: verdicts */}
        <section className="space-y-3">
          {(record.verdicts ?? []).map((v) => (
            <VerdictCard key={v.field} verdict={v} />
          ))}
          <WarningCard record={record} />
        </section>

        {/* Right: crops */}
        <CropViewer record={record} />
      </div>

      {/* Action bar */}
      <footer className="sticky bottom-0 mt-8 rounded-2xl border border-stone-200 bg-white p-4 shadow-lg">
        <div className="flex flex-wrap items-center gap-3">
          <input
            value={agent}
            onChange={(e) => setAgent(e.target.value)}
            placeholder="Your name / initials"
            className="w-44 rounded-lg border border-stone-300 px-3 py-2 text-sm"
          />
          <input
            value={note}
            onChange={(e) => setNote(e.target.value)}
            placeholder="Note (optional)"
            className="min-w-40 flex-1 rounded-lg border border-stone-300 px-3 py-2 text-sm"
          />
          <button
            disabled={saving}
            onClick={() => act('Rejected')}
            className="rounded-lg bg-red-600 px-5 py-2 text-sm font-medium text-white hover:bg-red-700 disabled:opacity-50"
          >
            Reject
          </button>
          <button
            disabled={saving}
            onClick={() => act('Approved')}
            className="rounded-lg bg-green-600 px-5 py-2 text-sm font-medium text-white hover:bg-green-700 disabled:opacity-50"
          >
            Approve
          </button>
        </div>
        <div className="mt-2 flex items-center justify-between text-xs text-stone-500">
          <span>
            {record.disposition
              ? `Currently ${record.disposition} by ${record.dispositioned_by} — acting again overrides it.`
              : 'This record is open — your call decides it.'}
          </span>
          {nextOpen && <span>Next open record loads automatically after you act.</span>}
        </div>
        {error && <p className="mt-2 text-sm text-red-700">{error}</p>}
      </footer>
    </div>
  )
}

function AutoStatusBadge({ record }: { record: RecordRow }) {
  const status = record.auto_status
  const style =
    status === 'Pass'
      ? 'bg-green-100 text-green-800'
      : status === 'Fail'
        ? 'bg-red-100 text-red-800'
        : 'bg-amber-100 text-amber-800'
  return (
    <div className="text-right">
      <span className={`rounded-full px-3 py-1 text-sm font-medium ${style}`}>
        {status === 'Needs Review' ? 'Needs review' : status}
      </span>
      <p className="mt-1 text-xs text-stone-500">automatic check result</p>
    </div>
  )
}

function outcomeStyle(v: Verdict): string {
  switch (v.outcome) {
    case 'exact':
      return 'border-green-200 bg-green-50/40'
    case 'mismatch':
      return 'border-red-300 bg-red-50/50'
    default:
      return 'border-amber-300 bg-amber-50/50'
  }
}

function VerdictCard({ verdict: v }: { verdict: Verdict }) {
  return (
    <div className={`rounded-xl border p-4 ${outcomeStyle(v)}`}>
      <div className="flex items-baseline justify-between">
        <h3 className="font-medium">{fieldLabel(v.field)}</h3>
        <span className="text-sm text-stone-600">{verdictSentence(v)}</span>
      </div>
      <dl className="mt-2 grid grid-cols-2 gap-3 text-sm">
        <div>
          <dt className="text-xs uppercase tracking-wide text-stone-500">On the form</dt>
          <dd className="mt-0.5 font-mono">
            {v.form_value ?? <span className="text-stone-400">not on this form revision</span>}
          </dd>
        </div>
        <div>
          <dt className="text-xs uppercase tracking-wide text-stone-500">
            Read from label{v.normalized && ' (normalized)'}
          </dt>
          <dd className="mt-0.5 font-mono">
            {v.label_value ?? <span className="text-stone-400">nothing found</span>}
          </dd>
        </div>
      </dl>
    </div>
  )
}

function WarningCard({ record }: { record: RecordRow }) {
  const w = record.warning
  const ok = w?.status === 'exact'
  return (
    <div
      className={`rounded-xl border p-4 ${
        ok ? 'border-green-200 bg-green-50/40' : 'border-amber-300 bg-amber-50/50'
      }`}
    >
      <div className="flex items-baseline justify-between">
        <h3 className="font-medium">Government warning</h3>
        <span className="text-sm text-stone-600">{warningHeadline(w)}</span>
      </div>
      {w?.found_text ? (
        <p className="mt-2 rounded-lg bg-white/70 p-3 font-mono text-xs leading-relaxed text-stone-700">
          {w.found_text}
        </p>
      ) : (
        <p className="mt-2 text-sm text-stone-500">
          Nothing legible was extracted — check the label image on the right.
        </p>
      )}
    </div>
  )
}

function CropViewer({ record }: { record: RecordRow }) {
  const crops = record.crops ?? []
  const [selected, setSelected] = useState<number | 'pdf'>(crops.length ? crops[0].index : 'pdf')
  const [zoom, setZoom] = useState(false)
  const crop = selected === 'pdf' ? null : crops.find((c) => c.index === selected)

  useEffect(() => setZoom(false), [selected])

  const buttonStyle = (active: boolean) =>
    `rounded-lg px-3 py-1 text-sm ${
      active
        ? 'bg-stone-900 text-white'
        : 'bg-white text-stone-700 ring-1 ring-stone-300 hover:bg-stone-50'
    }`

  return (
    <section className="flex flex-col">
      {crop ? (
        <div className="overflow-auto rounded-xl border border-stone-200 bg-white p-2" style={{ maxHeight: '70vh' }}>
          <img
            src={cropUrl(record.id, crop.index)}
            alt={crop.caption_type}
            onClick={() => setZoom((z) => !z)}
            className={`mx-auto cursor-zoom-in ${zoom ? 'w-[250%] max-w-none cursor-zoom-out' : 'max-h-[66vh] object-contain'}`}
          />
        </div>
      ) : (
        <iframe
          src={pdfUrl(record.id)}
          title="Full application PDF"
          className="h-[70vh] w-full rounded-xl border border-stone-200 bg-white"
        />
      )}
      <div className="mt-2 flex items-center justify-between">
        <div className="flex gap-2">
          {crops.map((c) => (
            <button
              key={c.index}
              onClick={() => setSelected(c.index)}
              className={`capitalize ${buttonStyle(c.index === selected)}`}
            >
              {c.kind === 'other' ? c.caption_type.toLowerCase() : c.kind}
            </button>
          ))}
          <button onClick={() => setSelected('pdf')} className={buttonStyle(selected === 'pdf')}>
            PDF
          </button>
        </div>
        <p className="text-xs text-stone-500">
          {crop
            ? `${crop.width_in}″ × ${crop.height_in}″ · click image to zoom`
            : crops.length
              ? 'the application as uploaded'
              : 'no label images were found — showing the uploaded PDF'}
        </p>
      </div>
    </section>
  )
}
