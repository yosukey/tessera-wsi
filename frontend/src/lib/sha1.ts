// Synchronous SHA-1 of a string's UTF-8 bytes, as lowercase hex.
//
// Only used to mirror the short name hash the server's safe_filename appends
// (admin_helpers.py) — not for anything security-relevant. crypto.subtle is
// not an option: it is async and, like crypto.randomUUID, missing outside
// secure contexts (plain-HTTP LAN installs).
export function sha1Hex(text: string): string {
  const bytes = new TextEncoder().encode(text)
  const bitLen = bytes.length * 8
  // Message + 0x80 + zero padding + 64-bit big-endian length, to a 64-byte multiple.
  const total = (((bytes.length + 8) >> 6) + 1) << 6
  const buf = new Uint8Array(total)
  buf.set(bytes)
  buf[bytes.length] = 0x80
  const view = new DataView(buf.buffer)
  view.setUint32(total - 8, Math.floor(bitLen / 0x100000000))
  view.setUint32(total - 4, bitLen >>> 0)

  let h0 = 0x67452301, h1 = 0xefcdab89, h2 = 0x98badcfe, h3 = 0x10325476, h4 = 0xc3d2e1f0
  const w = new Uint32Array(80)
  const rotl = (x: number, n: number) => (x << n) | (x >>> (32 - n))
  for (let off = 0; off < total; off += 64) {
    for (let i = 0; i < 16; i++) w[i] = view.getUint32(off + i * 4)
    for (let i = 16; i < 80; i++) w[i] = rotl(w[i - 3] ^ w[i - 8] ^ w[i - 14] ^ w[i - 16], 1)
    let a = h0, b = h1, c = h2, d = h3, e = h4
    for (let i = 0; i < 80; i++) {
      let f: number, k: number
      if (i < 20) { f = (b & c) | (~b & d); k = 0x5a827999 }
      else if (i < 40) { f = b ^ c ^ d; k = 0x6ed9eba1 }
      else if (i < 60) { f = (b & c) | (b & d) | (c & d); k = 0x8f1bbcdc }
      else { f = b ^ c ^ d; k = 0xca62c1d6 }
      const tmp = (rotl(a, 5) + f + e + k + w[i]) >>> 0
      e = d; d = c; c = rotl(b, 30) >>> 0; b = a; a = tmp
    }
    h0 = (h0 + a) >>> 0; h1 = (h1 + b) >>> 0; h2 = (h2 + c) >>> 0
    h3 = (h3 + d) >>> 0; h4 = (h4 + e) >>> 0
  }
  return [h0, h1, h2, h3, h4].map((h) => h.toString(16).padStart(8, '0')).join('')
}
