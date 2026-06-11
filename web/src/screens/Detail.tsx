import { useEffect, useMemo, useState } from 'react'
import { Link, useNavigate, useParams } from 'react-router-dom'
import {
  cropUrl,
  getRecord,
  listRecords,
  pdfUrl,
  setDisposition,
  type Box,
  type RecordRow,
  type Verdict,
} from '../api'
import {
  agentName,
  devMode,
  fieldLabel,
  setAgentName,
  verdictSentence,
  warningHeadline,
} from '../plain'

// Reader names. Agents see the short tag next to "Read from label";
// the dev attribution line spells out the form fallback.
const READER_TAGS: Record<string, string> = {
  ocr: 'OCR',
  vision: 'Vision',
  form: 'Form',
}

const SOURCE_LABELS: Record<string, string> = {
  ...READER_TAGS,
  form: 'Form (container wording)',
}

const sourceLabel = (s?: string | null) => (s ? (SOURCE_LABELS[s] ?? s) : 'unknown')

export default function Detail() {
  const { recordId } = useParams()
  const navigate = useNavigate()
  const [record, setRecord] = useState<RecordRow | null>(null)
  const [siblings, setSiblings] = useState<RecordRow[]>([])
  const [note, setNote] = useState('')
  const [agent, setAgent] = useState(agentName())
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)
  // Toggled from the upload page (bottom-left "Dev Mode"); read per visit.
  const [dev] = useState(devMode())
  // null = "no explicit choice yet": the viewer falls back to the first
  // crop. Verdict cards set it so "crop N" refs select the crop they cite.
  const [selectedCrop, setSelectedCrop] = useState<number | 'pdf' | null>(null)
  // Where the clicked card's value was read, drawn over the crop image.
  const [highlight, setHighlight] = useState<{ crop: number; box: Box } | null>(null)

  function showOnCrop(crop: number, box?: Box | null) {
    setSelectedCrop(crop)
    setHighlight(box ? { crop, box } : null)
  }

  useEffect(() => {
    if (!recordId) return
    setNote('')
    setError(null)
    setSaving(false)
    setSelectedCrop(null)
    setHighlight(null)
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
      <header className="mb-6 flex flex-wrap items-center justify-between gap-3">
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
        <div className="flex shrink-0 items-start gap-4">
          <a
            href={`/api/records/${record.id}/export.pdf`}
            className="whitespace-nowrap rounded-lg bg-white px-3 py-1.5 text-sm text-stone-700 ring-1 ring-stone-300 hover:bg-stone-50"
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
            <VerdictCard key={v.field} verdict={v} dev={dev} onShowCrop={showOnCrop} />
          ))}
          <WarningCard record={record} dev={dev} onShowCrop={showOnCrop} />
          {dev && <EscalationCard record={record} />}
        </section>

        {/* Right: crops */}
        <CropViewer
          record={record}
          dev={dev}
          selected={selectedCrop}
          onSelect={(s) => {
            setSelectedCrop(s)
            setHighlight(null)
          }}
          highlight={highlight}
        />
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
      <span className={`whitespace-nowrap rounded-full px-3 py-1 text-sm font-medium ${style}`}>
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

type ShowOnCrop = (crop: number, box?: Box | null) => void

function CropRef({
  index,
  box,
  onShowCrop,
}: {
  index: number
  box?: Box | null
  onShowCrop: ShowOnCrop
}) {
  return (
    <button
      onClick={(e) => {
        e.stopPropagation()
        onShowCrop(index, box)
      }}
      title="Show this crop in the viewer"
      className="underline decoration-dotted underline-offset-2 hover:text-stone-700"
    >
      crop {index}
    </button>
  )
}

function VerdictCard({
  verdict: v,
  dev,
  onShowCrop,
}: {
  verdict: Verdict
  dev: boolean
  onShowCrop: ShowOnCrop
}) {
  const clickable = v.source_crop != null
  return (
    <div
      onClick={clickable ? () => onShowCrop(v.source_crop!, v.box) : undefined}
      title={clickable ? 'Show where this was read on the label' : undefined}
      className={`rounded-xl border p-4 ${outcomeStyle(v)} ${
        clickable ? 'cursor-pointer transition-shadow hover:shadow' : ''
      }`}
    >
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
            Read from label
            {(() => {
              const tags = [
                v.label_value && v.source ? READER_TAGS[v.source] : null,
                v.normalized ? 'normalized' : null,
              ].filter(Boolean)
              return tags.length ? ` (${tags.join(', ')})` : ''
            })()}
          </dt>
          <dd className="mt-0.5 font-mono">
            {v.label_value ?? <span className="text-stone-400">nothing found</span>}
          </dd>
          {dev && (
            <p className="mt-1 font-mono text-xs text-stone-500">
              {v.label_value ? sourceLabel(v.source) : '—'}
              {v.source_crop != null && (
                <> · <CropRef index={v.source_crop} box={v.box} onShowCrop={onShowCrop} /></>
              )}
              {v.score != null && <> · score {v.score.toFixed(1)}</>}
              {v.normalized && ' · normalized'}
            </p>
          )}
        </div>
      </dl>
    </div>
  )
}

function WarningCard({
  record,
  dev,
  onShowCrop,
}: {
  record: RecordRow
  dev: boolean
  onShowCrop: ShowOnCrop
}) {
  const w = record.warning
  const ok = w?.status === 'exact'
  const clickable = w?.source_crop != null
  return (
    <div
      onClick={clickable ? () => onShowCrop(w!.source_crop!, w!.box) : undefined}
      title={clickable ? 'Show where this was read on the label' : undefined}
      className={`rounded-xl border p-4 ${
        ok ? 'border-green-200 bg-green-50/40' : 'border-amber-300 bg-amber-50/50'
      } ${clickable ? 'cursor-pointer transition-shadow hover:shadow' : ''}`}
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
      {w?.note && <p className="mt-2 text-sm text-stone-600">{w.note}</p>}
      {dev && w && (
        <p className="mt-2 font-mono text-xs text-stone-500">
          read by: {w.found_text ? sourceLabel(w.source) : '—'}
          {w.source_crop != null && (
            <> · <CropRef index={w.source_crop} box={w.box} onShowCrop={onShowCrop} /></>
          )}
          {' · score '}
          {w.score.toFixed(1)}
        </p>
      )}
    </div>
  )
}

function EscalationCard({ record }: { record: RecordRow }) {
  return (
    <div className="rounded-xl border border-stone-200 bg-stone-50/60 p-4">
      <h3 className="text-xs font-medium uppercase tracking-wide text-stone-500">
        Escalation reasons
      </h3>
      {record.escalation?.length ? (
        <ul className="mt-2 space-y-1 font-mono text-xs text-stone-500">
          {record.escalation.map((r, i) => (
            <li key={i}>{r}</li>
          ))}
        </ul>
      ) : (
        <p className="mt-2 text-xs text-stone-400">none — Tier A resolved everything</p>
      )}
    </div>
  )
}

function CropViewer({
  record,
  dev,
  selected: selectedProp,
  onSelect,
  highlight,
}: {
  record: RecordRow
  dev: boolean
  selected: number | 'pdf' | null
  onSelect: (s: number | 'pdf') => void
  highlight: { crop: number; box: Box } | null
}) {
  const crops = record.crops ?? []
  const selected = selectedProp ?? (crops.length ? crops[0].index : 'pdf')
  const [zoom, setZoom] = useState(false)
  const crop = selected === 'pdf' ? null : crops.find((c) => c.index === selected)
  const box = highlight && highlight.crop === selected ? highlight.box : null

  useEffect(() => setZoom(false), [selected])

  const buttonStyle = (active: boolean) =>
    `rounded-lg px-3 py-1 text-sm ${
      active
        ? 'bg-stone-900 text-white'
        : 'bg-white text-stone-700 ring-1 ring-stone-300 hover:bg-stone-50'
    }`

  return (
    <section className="flex flex-col">
      <div className="mb-2 flex flex-wrap items-center justify-between gap-2">
        <div className="flex flex-wrap gap-2">
          {crops.map((c) => (
            <button
              key={c.index}
              onClick={() => onSelect(c.index)}
              className={`capitalize ${buttonStyle(c.index === selected)}`}
            >
              {c.kind === 'other' ? c.caption_type.toLowerCase() : c.kind}
              {dev && <span className="opacity-60"> · {c.index}</span>}
            </button>
          ))}
          <button onClick={() => onSelect('pdf')} className={buttonStyle(selected === 'pdf')}>
            PDF
          </button>
        </div>
        <div className="text-right">
          <p className="text-xs text-stone-500">
            {crop
              ? `${crop.width_in}″ × ${crop.height_in}″ · click image to zoom`
              : crops.length
                ? 'the application as uploaded'
                : 'no label images were found — showing the uploaded PDF'}
          </p>
          {dev && crop && (
            <p className="mt-1 font-mono text-xs text-stone-400">
              ocr conf {crop.ocr_conf ?? '—'} · {crop.dpi} dpi ·{' '}
              {crop.vision_ok == null
                ? 'Vision not run'
                : crop.vision_ok
                  ? `Vision ok ${crop.vision_ms}ms`
                  : `Vision failed ${crop.vision_ms}ms${crop.vision_error ? ` (${crop.vision_error})` : ''}`}
            </p>
          )}
        </div>
      </div>
      {crop ? (
        <div className="overflow-auto rounded-xl border border-stone-200 bg-white p-2" style={{ maxHeight: '70vh' }}>
          {/* The wrapper hugs the img exactly, so the percent-positioned
              highlight tracks the image at any zoom level. */}
          <div className={`relative ${zoom ? 'w-[250%] max-w-none' : 'mx-auto w-fit'}`}>
            <img
              src={cropUrl(record.id, crop.index)}
              alt={crop.caption_type}
              onClick={() => setZoom((z) => !z)}
              className={`block ${zoom ? 'w-full cursor-zoom-out' : 'max-h-[66vh] cursor-zoom-in'}`}
            />
            {box && (
              <div
                className="pointer-events-none absolute rounded-sm border-2 border-amber-500 bg-amber-400/15"
                style={{
                  left: `${box[0] * 100}%`,
                  top: `${box[1] * 100}%`,
                  width: `${(box[2] - box[0]) * 100}%`,
                  height: `${(box[3] - box[1]) * 100}%`,
                }}
              />
            )}
          </div>
        </div>
      ) : (
        <iframe
          src={pdfUrl(record.id)}
          title="Full application PDF"
          className="h-[70vh] w-full rounded-xl border border-stone-200 bg-white"
        />
      )}
    </section>
  )
}
