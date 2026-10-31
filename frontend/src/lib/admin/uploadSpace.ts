import type { UploadSpaceInfo } from '../api/admin'

// Client-side copy of the server's slide-upload free-space check, so the bulk
// upload screen warns about (and blocks) exactly the batches the server would
// refuse, file by file, instead of guessing with a rule of its own.
//
// The server (services/app/wsi_app/main.py's pre-body check →
// admin_helpers.upload_space_short, with disk_space.reserve_for) refuses a
// slide upload with 507 uploadNoSpace before reading its body when, for the
// declared body size d:
//   - wsi/ and the spool share a filesystem: copies × d (the multipart spool +
//     the staged copy under wsi/) plus reserve_for(copies × d) exceeds its free
//     space;
//   - wsi/ is on a volume of its own: d + reserve_for(d) exceeds wsi/'s free
//     space, or (copies − 1) × d + reserve_for((copies − 1) × d) exceeds the
//     spool's.
// reserve_for(n) = max(floor(n × reserve_fraction), reserve_min_bytes). The
// endpoint checks once more after the body is spooled (factor 1), but that
// check is implied by the first in both layouts, so the first is the one to
// predict. GET /api/admin/storage reports every input (upload_space): the
// two volumes' free bytes measured where the check measures them, the copy
// count and the reserve policy — nothing here is a hard-coded server number
// that could drift.
//
// The queue uploads one file at a time (UploadQueue.start), and once a file is
// in, its spool copy is deleted and only the staged WSI stays on wsi/'s
// volume. So file i is checked against the free space the server will see
// when it gets there: the reported figure minus the sizes of the files before
// it. A batch whose total would fit a plain "sum + 1 GiB" rule can still have
// its later files refused (each needs ~2x itself plus a reserve on top of
// what the earlier ones left), and one that rule blocked can be fine.
//
// Advisory like the server's own check: other writers (thumbnails, caches,
// the DB, concurrent uploads) move the free figure in the meantime.

// Upper bound on the multipart envelope around the file in the request body —
// what the server's Content-Length counts besides the file (the check reads
// the declared body, not the file size): two boundary lines (RFC 2046 caps a
// boundary at 70 characters), the Content-Disposition and Content-Type part
// headers, and the filename, which a browser may percent-escape (up to 3
// bytes per UTF-8 byte). Erring a few hundred bytes high only matters for a
// file within those bytes of the limit; erring low would wave through a
// file the server refuses.
const MULTIPART_OVERHEAD_BYTES = 512

const utf8 = new TextEncoder()

export function multipartDeclaredBytes(file: { name: string; size: number }): number {
  return file.size + MULTIPART_OVERHEAD_BYTES + 3 * utf8.encode(file.name).length
}

// disk_space.reserve_for. Python's int(n * 0.1) truncates the same IEEE double
// product Math.floor does here (n ≥ 0), so the two agree to the byte.
export function reserveFor(nbytes: number, space: Pick<UploadSpaceInfo, 'reserve_fraction' | 'reserve_min_bytes'>): number {
  return Math.max(Math.floor(Math.max(0, nbytes) * space.reserve_fraction), space.reserve_min_bytes)
}

export interface UploadSpaceShortfall {
  // The first file, in queue order, the server would refuse.
  index: number
  name: string
  // Bytes to free (on the volumes together) for every file of the batch to
  // pass, each one after the earlier ones have gone in.
  shortfallBytes: number
}

// Simulate the batch in upload order. Returns null when every file fits, or
// when nothing can be known (no upload_space, or both volumes unknown): an
// unknown volume passes the server's check, so it never blocks here either.
// `files` must be only those the queue will actually send (its own
// pre-validation fails wrong extensions / oversize files without a request).
export function simulateUploadSpace(
  files: ReadonlyArray<{ name: string; size: number }>,
  space: UploadSpaceInfo | null | undefined,
): UploadSpaceShortfall | null {
  if (!space || files.length === 0) return null
  const copies = Math.max(1, space.copies)
  const target = space.target_free_bytes
  const spool = space.same_volume ? null : space.spool_free_bytes
  const need = (n: number) => n + reserveFor(n, space)

  let first: { index: number; name: string } | null = null
  // Worst deficit over the batch, per volume: freeing that much on it makes
  // every file fit there.
  let targetDeficit = 0
  let spoolDeficit = 0
  let staged = 0 // bytes earlier files left on the target volume
  for (let index = 0; index < files.length; index++) {
    const f = files[index]
    const declared = multipartDeclaredBytes(f)
    let short = false
    if (target != null) {
      const required = space.same_volume ? need(copies * declared) : need(declared)
      const deficit = required - (target - staged)
      if (deficit > 0) { short = true; targetDeficit = Math.max(targetDeficit, deficit) }
    }
    if (spool != null && copies > 1) {
      // The spool copy is deleted after each upload: no depletion here.
      const deficit = need((copies - 1) * declared) - spool
      if (deficit > 0) { short = true; spoolDeficit = Math.max(spoolDeficit, deficit) }
    }
    if (short && first == null) first = { index, name: f.name }
    staged += f.size
  }
  if (first == null) return null
  return { ...first, shortfallBytes: targetDeficit + spoolDeficit }
}
