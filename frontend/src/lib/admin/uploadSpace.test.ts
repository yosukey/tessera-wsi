import { describe, expect, it } from 'vitest'
import type { UploadSpaceInfo } from '../api/admin'
import { multipartDeclaredBytes, reserveFor, simulateUploadSpace } from './uploadSpace'

const MiB = 1024 ** 2
const GiB = 1024 ** 3
// disk_space.RESERVE_FRACTION / RESERVE_MIN_BYTES, admin_helpers.SLIDE_UPLOAD_COPIES
// (what GET /api/admin/storage reports; the backend test pins those).
const POLICY = { copies: 2, reserve_fraction: 0.1, reserve_min_bytes: 256 * MiB }

function sameVolume(free: number | null): UploadSpaceInfo {
  return { ...POLICY, same_volume: true, target_free_bytes: free, spool_free_bytes: free }
}
function split(target: number | null, spool: number | null): UploadSpaceInfo {
  return { ...POLICY, same_volume: false, target_free_bytes: target, spool_free_bytes: spool }
}
const file = (name: string, size: number) => ({ name, size })
const need = (n: number) => n + reserveFor(n, POLICY)

describe('reserveFor', () => {
  it('is 10 % of the write, truncated, and at least 256 MiB (disk_space.reserve_for)', () => {
    expect(reserveFor(0, POLICY)).toBe(256 * MiB)
    expect(reserveFor(-5, POLICY)).toBe(256 * MiB)
    expect(reserveFor(10 * GiB, POLICY)).toBe(GiB)
    expect(reserveFor(10 * GiB + 9, POLICY)).toBe(GiB) // floor, as Python's int()
    expect(reserveFor(2560 * MiB - 10, POLICY)).toBe(256 * MiB) // floor still wins below it
  })
})

describe('multipartDeclaredBytes', () => {
  it('adds the multipart envelope, counting the filename in UTF-8 bytes', () => {
    const ascii = multipartDeclaredBytes(file('a.svs', 1000))
    expect(ascii).toBeGreaterThan(1000 + 5)
    // "é" is 2 UTF-8 bytes: one more character, two more bytes, escaped up to 3x each.
    expect(multipartDeclaredBytes(file('é.svs', 1000)) - multipartDeclaredBytes(file('e.svs', 1000))).toBe(3)
  })
})

describe('simulateUploadSpace — one volume', () => {
  const f = file('a.svs', 5 * GiB)
  const exact = need(2 * multipartDeclaredBytes(f))

  it('passes an exact fit and refuses one byte short, naming the file', () => {
    expect(simulateUploadSpace([f], sameVolume(exact))).toBeNull()
    expect(simulateUploadSpace([f], sameVolume(exact - 1))).toEqual({ index: 0, name: 'a.svs', shortfallBytes: 1 })
  })

  it('takes each file\'s staged bytes off the space the next one sees', () => {
    const a = file('a.svs', 3 * GiB)
    const b = file('b.svs', 2 * GiB)
    // b runs after a's WSI landed: it needs its own 2x + reserve on top of a's size.
    const exactB = a.size + need(2 * multipartDeclaredBytes(b))
    expect(exactB).toBeGreaterThan(need(2 * multipartDeclaredBytes(a))) // b is the binding file
    expect(simulateUploadSpace([a, b], sameVolume(exactB))).toBeNull()
    expect(simulateUploadSpace([a, b], sameVolume(exactB - 1))).toEqual({ index: 1, name: 'b.svs', shortfallBytes: 1 })
  })

  it('refuses a batch a plain "total + 1 GiB" rule would let through', () => {
    // Four 2 GiB files: 8 GiB + 1 GiB margin = 9 GiB "fit", but the last one
    // still needs 2x itself plus the reserve after 6 GiB are staged.
    const batch = ['a', 'b', 'c', 'd'].map((n) => file(`${n}.svs`, 2 * GiB))
    const r = simulateUploadSpace(batch, sameVolume(9 * GiB))
    expect(r?.index).toBe(3)
    expect(r?.name).toBe('d.svs')
  })

  it('reports the first refused file and the worst deficit across the batch', () => {
    const big = file('big.svs', 4 * GiB)
    const small = file('small.svs', 10 * MiB)
    const free = need(2 * multipartDeclaredBytes(big)) - 100
    const r = simulateUploadSpace([big, small], sameVolume(free))
    expect(r).toEqual({ index: 0, name: 'big.svs', shortfallBytes: 100 })
    // Freeing the reported shortfall makes the whole batch pass.
    expect(simulateUploadSpace([big, small], sameVolume(free + 100))).toBeNull()
  })
})

describe('simulateUploadSpace — wsi/ on its own volume', () => {
  const f = file('a.svs', 5 * GiB)
  const once = need(multipartDeclaredBytes(f))

  it('needs the body once on each volume, with the reserve, at the byte', () => {
    expect(simulateUploadSpace([f], split(once, once))).toBeNull()
    expect(simulateUploadSpace([f], split(once - 1, once))).toEqual({ index: 0, name: 'a.svs', shortfallBytes: 1 })
    expect(simulateUploadSpace([f], split(once, once - 1))).toEqual({ index: 0, name: 'a.svs', shortfallBytes: 1 })
    // Both short: the bytes to free on each add up.
    expect(simulateUploadSpace([f], split(once - 1, once - 2))?.shortfallBytes).toBe(3)
  })

  it('depletes the wsi volume across the batch but not the spool', () => {
    const batch = [file('a.svs', 5 * GiB), file('b.svs', 5 * GiB), file('c.svs', 5 * GiB)]
    // The spool copy is deleted after each upload: room for one is enough.
    expect(simulateUploadSpace(batch, split(10_000 * GiB, once))).toBeNull()
    const exactTarget = 2 * f.size + once
    expect(simulateUploadSpace(batch, split(exactTarget, once))).toBeNull()
    expect(simulateUploadSpace(batch, split(exactTarget - 1, once))).toEqual({ index: 2, name: 'c.svs', shortfallBytes: 1 })
  })
})

describe('simulateUploadSpace — unknown free space never blocks', () => {
  const f = file('a.svs', 5 * GiB)

  it('passes with no figures at all (not loaded / failed fetch)', () => {
    expect(simulateUploadSpace([f], undefined)).toBeNull()
    expect(simulateUploadSpace([f], null)).toBeNull()
  })

  it('passes on a volume the server cannot measure, still checking the other', () => {
    expect(simulateUploadSpace([f], sameVolume(null))).toBeNull()
    expect(simulateUploadSpace([f], split(null, null))).toBeNull()
    expect(simulateUploadSpace([f], split(null, 1))?.index).toBe(0) // spool known and short
    expect(simulateUploadSpace([f], split(1, null))?.index).toBe(0) // wsi/ known and short
  })

  it('has nothing to check for an empty batch', () => {
    expect(simulateUploadSpace([], sameVolume(1))).toBeNull()
  })
})
