// Plain-language mappings. Confidence scores route records under the
// hood and are never shown to the agent.

import type { RecordRow, Verdict, WarningResult } from './api'

export const FIELD_LABELS: Record<string, string> = {
  brand_name: 'Brand name',
  net_contents: 'Net contents',
  alcohol_content: 'Alcohol content',
  class_type: 'Class / type',
}

export function fieldLabel(field: string): string {
  return FIELD_LABELS[field] ?? field
}

const formatCheck = (v: Verdict) => v.note?.includes('not on form')

export function verdictSentence(v: Verdict): string {
  const name = fieldLabel(v.field)
  switch (v.outcome) {
    case 'exact':
      if (v.note?.includes('stated on container'))
        return 'Matches — stated on the container itself (noted on the form)'
      if (formatCheck(v)) return 'Found on label and looks reasonable'
      return 'Matches the form'
    case 'near_miss':
      return 'Close to the form but not identical — please check'
    case 'mismatch':
      return `Label disagrees with the form — compare ${name.toLowerCase()} on the label`
    case 'missing':
      if (formatCheck(v)) return "Couldn't find this on the label — please verify"
      return "Couldn't find this on the label — please verify"
  }
}

export function warningHeadline(w: WarningResult | null): string {
  if (!w) return "Couldn't read clearly — please verify"
  switch (w.status) {
    case 'exact':
      return 'Present, exact required wording'
    case 'prefix_not_caps':
      return 'Wording correct, but "GOVERNMENT WARNING:" is not in all capitals'
    case 'near':
      return 'Almost matches the required wording — please verify against the label'
    case 'mismatch':
      return 'Differs from the required wording'
    case 'missing':
      return "Couldn't read a government warning — please verify on the label"
  }
}

// Short reasons for queue rows: only what needs the agent's eyes.
export function recordReasons(r: RecordRow): string[] {
  if (r.state === 'error') return [r.error ?? 'Could not process this PDF']
  const reasons: string[] = []
  for (const v of r.verdicts ?? []) {
    if (v.outcome === 'exact') continue
    const name = fieldLabel(v.field)
    if (v.outcome === 'near_miss') reasons.push(`${name}: close but not identical`)
    else if (v.outcome === 'mismatch') reasons.push(`${name}: doesn't match the form`)
    else reasons.push(`${name}: couldn't find it on the label`)
  }
  const w = r.warning
  if (w && w.status !== 'exact') {
    if (w.status === 'near') reasons.push('Government warning: almost matches — verify wording')
    else if (w.status === 'prefix_not_caps')
      reasons.push('Government warning: prefix not in all capitals')
    else if (w.status === 'mismatch') reasons.push('Government warning: wording differs')
    else reasons.push("Government warning: couldn't read one")
  }
  return reasons
}

export function agentName(): string {
  return localStorage.getItem('cola-proof.agent') ?? ''
}

export function setAgentName(name: string) {
  localStorage.setItem('cola-proof.agent', name)
}
