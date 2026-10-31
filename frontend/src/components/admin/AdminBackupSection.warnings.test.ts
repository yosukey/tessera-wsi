import { describe, expect, it } from 'vitest'
import ja from '../../lib/i18n/ja'
import { backupWarningLines } from './AdminBackupSection'

describe('backupWarningLines', () => {
  it('shows Japanese text for coded warnings, the sentence otherwise', () => {
    const msgs = ja as Record<string, string | ((p: Record<string, string | number>) => string)>
    const t = (k: string, p: Record<string, string | number> = {}) => {
      const m = msgs[k]
      if (!m) return k
      return typeof m === 'function' ? m(p) : m
    }
    const lines = backupWarningLines(t, {
      warnings: ['tour abc was deleted after the DB snapshot', 'old sentence', '…and 4 more (see the server log)'],
      warning_items: [
        { code: 'tourDeletedAfterSnapshot', params: { tour: 'abc' } },
        { code: 'unknown', params: {} },
        { code: 'more', params: { count: 4 } },
      ],
    })
    expect(lines[0]).toContain('ツアー abc')
    expect(lines[1]).toBe('old sentence')
    expect(lines[2]).toContain('4')
    expect(lines[2]).not.toContain('more')
  })

  it('handles uncoded or missing warnings', () => {
    const t = (k: string) => k
    expect(backupWarningLines(t, { warnings: ['a', 'b'] })).toEqual(['a', 'b'])
    expect(backupWarningLines(t, {})).toEqual([])
    expect(backupWarningLines(t, undefined)).toEqual([])
  })
})
