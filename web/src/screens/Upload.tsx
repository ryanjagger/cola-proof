import {
  CaretRight,
  CircleNotch,
  FilePdf,
  Terminal,
  WarningCircle,
} from '@phosphor-icons/react'
import { useCallback, useEffect, useState } from 'react'
import { Link, useNavigate } from 'react-router-dom'
import { listBatches, uploadBatch, type Batch } from '../api'
import { devMode, setDevMode } from '../plain'

export default function Upload() {
  const navigate = useNavigate()
  const [dragging, setDragging] = useState(false)
  const [uploading, setUploading] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [recent, setRecent] = useState<Batch[]>([])
  const [dev, setDev] = useState(devMode())

  useEffect(() => {
    listBatches().then(setRecent).catch(() => {})
  }, [])

  const submit = useCallback(
    async (fileList: FileList | File[]) => {
      const files = Array.from(fileList).filter((f) =>
        f.name.toLowerCase().endsWith('.pdf'),
      )
      if (!files.length) {
        setError('Those files are not PDFs — drop the COLA PDFs exported from the registry.')
        return
      }
      setUploading(true)
      setError(null)
      try {
        const { batch } = await uploadBatch(files)
        navigate(`/batches/${batch.id}`)
      } catch (e) {
        setUploading(false)
        setError(`Upload didn't go through. ${e instanceof Error ? e.message : ''}`)
      }
    },
    [navigate],
  )

  return (
    <div className="mx-auto flex min-h-screen max-w-3xl flex-col px-6 py-12">
      <header className="mb-10">
        <h1 className="text-2xl font-semibold tracking-tight">COLA Proof</h1>
        <p className="mt-1 text-stone-600">
          Drop one or more COLA application PDFs. Each label gets checked
          against its form; anything doubtful is flagged for your review.
        </p>
      </header>

      <label
        onDragOver={(e) => {
          e.preventDefault()
          setDragging(true)
        }}
        onDragLeave={() => setDragging(false)}
        onDrop={(e) => {
          e.preventDefault()
          setDragging(false)
          submit(e.dataTransfer.files)
        }}
        className={`flex h-72 cursor-pointer flex-col items-center justify-center rounded-2xl border-2 border-dashed transition-colors ${
          dragging
            ? 'border-blue-500 bg-blue-50'
            : 'border-stone-300 bg-white hover:border-stone-400'
        }`}
      >
        <input
          type="file"
          multiple
          accept=".pdf,application/pdf"
          className="hidden"
          disabled={uploading}
          onChange={(e) => e.target.files && submit(e.target.files)}
        />
        {uploading ? (
          <p className="flex items-center gap-2 text-lg text-stone-600">
            <CircleNotch size={20} className="animate-spin" aria-hidden />
            Uploading…
          </p>
        ) : (
          <>
            <FilePdf
              size={40}
              weight="duotone"
              aria-hidden
              className={dragging ? 'text-blue-500' : 'text-stone-400'}
            />
            <p className="mt-3 text-lg font-medium">Drop PDFs here</p>
            <p className="mt-1 text-sm text-stone-500">or click to choose files</p>
          </>
        )}
      </label>
      {error && (
        <p className="mt-4 flex items-center gap-1.5 text-sm text-red-700">
          <WarningCircle size={16} weight="fill" aria-hidden className="shrink-0" />
          {error}
        </p>
      )}

      {recent.length > 0 && (
        <section className="mt-12">
          <h2 className="text-sm font-medium uppercase tracking-wide text-stone-500">
            Recent batches
          </h2>
          <ul className="mt-3 divide-y divide-stone-200 rounded-xl border border-stone-200 bg-white">
            {recent.map((b) => (
              <li key={b.id}>
                <Link
                  to={`/batches/${b.id}`}
                  className="flex items-center justify-between px-4 py-3 hover:bg-stone-50"
                >
                  <span>{b.name}</span>
                  <span className="flex items-center gap-3 text-sm text-stone-500">
                    {b.summary
                      ? b.summary.complete
                        ? 'complete'
                        : `${b.summary.open} open`
                      : ''}
                    <span>{new Date(b.created_at).toLocaleString()}</span>
                    <CaretRight size={14} aria-hidden className="text-stone-400" />
                  </span>
                </Link>
              </li>
            ))}
          </ul>
        </section>
      )}

      <button
        onClick={() => {
          const next = !dev
          setDevMode(next)
          setDev(next)
        }}
        title="Show pipeline diagnostics on record pages (developer)"
        className={`fixed bottom-4 left-4 inline-flex items-center gap-1.5 rounded-lg px-3 py-1.5 text-sm ring-1 ${
          dev
            ? 'bg-stone-900 text-white ring-stone-900'
            : 'bg-white text-stone-400 ring-stone-200 hover:bg-stone-50'
        }`}
      >
        <Terminal size={14} aria-hidden />
        Dev Mode
      </button>
    </div>
  )
}
