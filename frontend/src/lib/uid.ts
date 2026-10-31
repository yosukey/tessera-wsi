// Crypto-backed identifier helper.
//
// Tour-step pins, shapes, and the like need a client-side unique id at the
// moment of creation (before the row exists on the server). The 12 hex chars
// taken from crypto.randomUUID / getRandomValues carry 48 random bits with no
// JS-level coordination, avoiding the non-zero collision rate of a
// `Date.now()-Math.random()` recipe when two ids are minted in the same
// millisecond — easy to hit in unit tests, harder but possible in production
// when two shapes are added back-to-back.
//
// Returns "<prefix>-<12 hex chars>" so the prefix scoping ("pin", "shape")
// stays for log readability and the suffix fits comfortably in the 64-char
// `pin_id` / `shape_id` columns enforced by the SQLAlchemy schema.

const HEX = '0123456789abcdef'

function randomHex(length: number): string {
  if (typeof crypto !== 'undefined' && typeof crypto.randomUUID === 'function') {
    // randomUUID is the cheapest path on every modern browser. It returns
    // 32 hex chars + 4 dashes; strip the dashes and slice down.
    return crypto.randomUUID().replace(/-/g, '').slice(0, length)
  }
  if (typeof crypto !== 'undefined' && crypto.getRandomValues) {
    const buf = new Uint8Array(Math.ceil(length / 2))
    crypto.getRandomValues(buf)
    let out = ''
    for (let i = 0; i < buf.length; i++) {
      out += HEX[(buf[i] >> 4) & 0xf] + HEX[buf[i] & 0xf]
    }
    return out.slice(0, length)
  }
  // This branch isn't expected to fire in any browser the project supports,
  // but Math.random gives a workable last resort.
  let out = ''
  for (let i = 0; i < length; i++) out += HEX[Math.floor(Math.random() * 16)]
  return out
}

export function makeUid(prefix: string): string {
  return `${prefix}-${randomHex(12)}`
}
