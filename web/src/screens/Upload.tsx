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
          <p className="text-lg text-stone-600">Uploading…</p>
        ) : (
          <>
            <p className="text-lg font-medium">Drop PDFs here</p>
            <p className="mt-1 text-sm text-stone-500">or click to choose files</p>
          </>
        )}
      </label>
      {error && <p className="mt-4 text-sm text-red-700">{error}</p>}

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
                  <span className="text-sm text-stone-500">
                    {b.summary
                      ? b.summary.complete
                        ? 'complete'
                        : `${b.summary.open} open`
                      : ''}
                    <span className="ml-3">{new Date(b.created_at).toLocaleString()}</span>
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
        className={`fixed bottom-4 left-4 rounded-lg px-3 py-1.5 text-sm ring-1 ${
          dev
            ? 'bg-stone-900 text-white ring-stone-900'
            : 'bg-white text-stone-400 ring-stone-200 hover:bg-stone-50'
        }`}
      >
        Dev Mode
      </button>
    </div>
  )
}
