import { describe, expect, it } from 'vitest'
import { SOURCE_URL_MAX_LEN, safeSourceUrl } from './slideCredit'
import cases from './slideCredit.sourceUrlCases.json'

// The same table drives services/app/tests/test_slide_credit.py against
// slide_credit.clean_source_url, so the modal never sends a URL the server
// refuses and the viewer links every URL the server stores (bar the IDNA
// cases listed as leftToTheBrowser).
function parses(url: string): boolean {
  try {
    new URL(url)
    return true
  } catch {
    return false
  }
}

describe('safeSourceUrl', () => {
  it.each(cases.accept)('accepts %s', (url) => {
    expect(safeSourceUrl(url)).toBe(url)
  })

  it.each(cases.refuse)('refuses %s', (url) => {
    expect(safeSourceUrl(url)).toBe('')
  })

  // Whether these parse is the URL parser's own IDNA (UTS 46) call, and
  // parsers disagree across engines and releases (Node 22 refuses xn--a.org
  // and accepts ١٢.org, Node 24.21 the reverse). So the outcome isn't pinned:
  // safeSourceUrl must follow the parser — link exactly what it accepts, with
  // no refusal of its own.
  it.each(cases.leftToTheBrowser)('leaves %s to the URL parser (IDNA)', (url) => {
    expect(safeSourceUrl(url)).toBe(parses(url) ? url : '')
  })

  it('returns the trimmed URL', () => {
    expect(safeSourceUrl('  https://example.org/x ')).toBe('https://example.org/x')
  })

  it('counts the length limit in characters, as the server does', () => {
    const base = 'https://example.org/'
    // 1024 characters, but 2028 UTF-16 units.
    const astral = base + '🔬'.repeat(SOURCE_URL_MAX_LEN - base.length)
    expect(safeSourceUrl(astral)).toBe(astral)
    expect(safeSourceUrl(astral + 'a')).toBe('')
  })
})
