import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

import { describe, expect, it } from 'vitest'

const TEST_DIR = dirname(fileURLToPath(import.meta.url))

function pluginSource(relativePath: string): string {
  return readFileSync(resolve(TEST_DIR, '..', '..', 'plugins', relativePath), 'utf8')
}

describe('Admin 4.7.2 UTC compatibility in local plugin pages', () => {
  it('uses the shared API-date parser for Smart Support correlation windows', () => {
    const source = pluginSource('smart-support/ReportPage.tsx')
    expect(source).toContain('parseApiDate(c.window_end)')
    expect(source).toContain('parseApiDate(c.window_start)')
    expect(source).not.toContain('new Date(c.window_')
  })

  it('uses the shared API-date formatter for Incident Center timestamps', () => {
    const source = pluginSource('incident-center/IncidentCenterPage.tsx')
    expect(source).toContain('formatDateUtil(incident.updated_at)')
    expect(source).not.toContain('new Date(incident.updated_at)')
  })

  it('uses the shared API-date parser for Block Radar history and timers', () => {
    const source = pluginSource('block-radar/RadarPage.tsx')
    expect(source).toContain('parseApiDate(target.sampled_at)')
    expect(source).toContain('parseApiDate(nextProbeAt)')
    expect(source).toContain('parseApiDate(iso)')
    expect(source).not.toContain('new Date(target.sampled_at)')
    expect(source).not.toContain('new Date(nextProbeAt)')
    expect(source).not.toContain('new Date(iso)')
  })
})
