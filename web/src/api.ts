export type Outcome = 'exact' | 'near_miss' | 'mismatch' | 'missing'

// Which reader produced a matched value; absent on records processed
// before source attribution existed.
export type VerdictSource = 'ocr' | 'vision' | 'form'

// (x0, y0, x1, y1) as fractions of the crop; scales with any display size.
export type Box = [number, number, number, number]

export interface Verdict {
  field: string
  form_value: string | null
  label_value: string | null
  outcome: Outcome
  score: number | null
  normalized: boolean
  note: string | null
  source?: VerdictSource | null
  source_crop?: number | null
  // Where label_value was read on the crop; OCR-sourced verdicts only.
  box?: Box | null
}

export interface WarningResult {
  status: 'exact' | 'prefix_not_caps' | 'near' | 'mismatch' | 'missing'
  found_text: string | null
  score: number
  note?: string | null // absent on records processed before notes existed
  source?: VerdictSource | null
  source_crop?: number | null
  box?: Box | null
}

export interface Crop {
  index: number
  kind: 'front' | 'back' | 'other' | 'photo'
  caption_type: string
  width_in: number
  height_in: number
  px_width: number
  px_height: number
  dpi: number
  ext: string
  filename: string
  ocr_conf: number | null
  // Tier B audit trail; null when the crop wasn't re-read.
  vision_ok?: boolean | null
  vision_ms?: number | null
  vision_error?: string | null
}

export interface FormFields {
  ttb_id: string | null
  revision: string | null
  serial_number: string | null
  brand_name: string | null
  fanciful_name: string | null
  applicant: string | null
  product_type: string | null
  source: string | null
  net_contents: string | null
  alcohol_content: string | null
  class_type_description: string | null
  status: string | null
  has_net_contents_field: boolean
  has_alcohol_content_field: boolean
  [key: string]: unknown
}

export interface RecordRow {
  id: string
  batch_id: string
  filename: string
  state: 'pending' | 'processing' | 'escalating' | 'done' | 'error'
  error: string | null
  ttb_id: string | null
  auto_status: 'Pass' | 'Needs Review' | 'Fail' | null
  disposition: 'Approved' | 'Rejected' | null
  dispositioned_by: string | null
  dispositioned_at: string | null
  note: string | null
  form: FormFields | null
  crops: Crop[] | null
  verdicts: Verdict[] | null
  warning: WarningResult | null
  escalation: string[] | null
}

export interface BatchSummary {
  total: number
  processed: number
  failed: number
  needs_review: number
  passed: number
  errors: number
  open: number
  complete: boolean
}

export interface Batch {
  id: string
  name: string
  created_at: string
  summary?: BatchSummary
}

async function check(resp: Response) {
  if (!resp.ok) throw new Error(`${resp.status}: ${await resp.text()}`)
  return resp
}

export async function uploadBatch(files: File[]): Promise<{ batch: Batch }> {
  const body = new FormData()
  files.forEach((f) => body.append('files', f))
  const resp = await check(await fetch('/api/batches', { method: 'POST', body }))
  return resp.json()
}

export async function getBatch(id: string): Promise<Batch & { summary: BatchSummary }> {
  return (await check(await fetch(`/api/batches/${id}`))).json()
}

export async function listBatches(): Promise<Batch[]> {
  return (await check(await fetch('/api/batches'))).json()
}

export async function listRecords(batchId: string): Promise<RecordRow[]> {
  return (await check(await fetch(`/api/batches/${batchId}/records`))).json()
}

export async function getRecord(id: string): Promise<RecordRow> {
  return (await check(await fetch(`/api/records/${id}`))).json()
}

export async function setDisposition(
  id: string,
  disposition: 'Approved' | 'Rejected',
  by: string,
  note?: string,
): Promise<RecordRow> {
  const resp = await check(
    await fetch(`/api/records/${id}/disposition`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ disposition, by, note: note || null }),
    }),
  )
  return resp.json()
}

export function cropUrl(recordId: string, index: number): string {
  return `/api/records/${recordId}/crops/${index}`
}

export function pdfUrl(recordId: string): string {
  return `/api/records/${recordId}/pdf`
}
