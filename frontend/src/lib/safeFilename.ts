import { sha1Hex } from './sha1'

// Mirror services/.../admin_helpers.py:safe_filename. The server
// canonicalises uploaded names (e.g. "f o o.png" -> "f_o_o.png",
// "図1.png" -> "1_<hash>.png") before writing to disk, so comparing the raw
// File.name against the cached asset list would miss a collision where the
// safe form clashes with an existing on-disk name, and an "overwrite" upload
// would silently replace the existing asset. Keep the two in lockstep.
const UNSAFE_CHARS_RE = /[^A-Za-z0-9._-]+/g
const FILENAME_MAX_BYTES = 200
// Longest extension kept whole when the name has to be cut (dot included).
const FILENAME_EXT_MAX_BYTES = 16
const WINDOWS_RESERVED = new Set([
  'CON', 'PRN', 'AUX', 'NUL',
  ...Array.from({ length: 9 }, (_, i) => `COM${i + 1}`),
  ...Array.from({ length: 9 }, (_, i) => `LPT${i + 1}`),
])

// os.path.splitext: the extension starts at the last dot, unless every
// character before that dot is itself a dot (".bashrc" has no extension).
function splitExt(name: string): [string, string] {
  const dot = name.lastIndexOf('.')
  let lead = 0
  while (lead < name.length && name[lead] === '.') lead++
  return dot > lead ? [name.slice(0, dot), name.slice(dot)] : [name, '']
}

export function clientSafeFilename(name: string): string {
  let raw = (name || '').trim()
  // Keep only the basename — clients can include a path on some browsers.
  const lastSlash = Math.max(raw.lastIndexOf('/'), raw.lastIndexOf('\\'))
  if (lastSlash >= 0) raw = raw.slice(lastSlash + 1)
  // Strip C0 controls and DEL outright (mirrors the server's `ch >= " "` test).
  raw = raw.replace(/[\x00-\x1f\x7f]/g, '')
  raw = raw.normalize('NFC')
  let [originalStem, rawExt] = splitExt(raw)
  if (rawExt) {
    // ".." is collapsed in the stem only, after the extension is split off,
    // and a trailing dot dropped, so "case12..svs" keeps its extension.
    originalStem = originalStem.replace(/\.\./g, '_').replace(/\.+$/, '')
  } else {
    // No extension, or a name of leading dots ("...svs"): ".." collapsed
    // over the whole name.
    ;[originalStem, rawExt] = splitExt(raw.replace(/\.\./g, '_'))
  }
  let stem = originalStem.replace(UNSAFE_CHARS_RE, '_').replace(/^[._-]+/, '')
  let ext = rawExt.replace(UNSAFE_CHARS_RE, '_')
  // eslint-disable-next-line no-control-regex
  let digest = /[^\x00-\x7f]/.test(originalStem) ? sha1Hex(originalStem.toLowerCase()).slice(0, 8) : ''
  if (!stem) {
    stem = `file${digest}`
    digest = ''
  }
  const suffix = digest ? `_${digest}` : ''
  if (ext.length > FILENAME_EXT_MAX_BYTES && stem.length + suffix.length + ext.length > FILENAME_MAX_BYTES) {
    ext = ext.slice(0, FILENAME_EXT_MAX_BYTES)
  }
  stem = stem.slice(0, Math.max(1, FILENAME_MAX_BYTES - ext.length - suffix.length)) + suffix
  if (WINDOWS_RESERVED.has(stem.toUpperCase())) stem = `_${stem}`
  return stem + ext
}
