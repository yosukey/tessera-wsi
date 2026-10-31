const MAX_LEN = 2048

/**
 * Validate a post-login return path taken from `?next=`. Only a same-origin
 * relative path is accepted (single leading slash, so no scheme or `//host`
 * authority, no backslash which browsers fold into `/`, no control chars);
 * anything else — and `/login` itself, to avoid a loop — yields `null`.
 * Mirrors `safe_next_path` in the backend, which re-validates for OAuth.
 */
export function sanitizeNext(raw: string | null | undefined): string | null {
  if (!raw || raw.length > MAX_LEN || !raw.startsWith('/')) return null
  if (raw.startsWith('//') || raw.includes('\\')) return null
  // eslint-disable-next-line no-control-regex
  if (/[\u0000-\u001f\u007f]/.test(raw)) return null
  if (/^\/login(?:$|[/?#])/.test(raw)) return null
  return raw
}

/** The `/login` URL that returns the visitor to `loc` after signing in. */
export function loginUrlFor(loc: { pathname: string; search: string; hash: string }): string {
  const target = sanitizeNext(`${loc.pathname}${loc.search}${loc.hash}`)
  return target && target !== '/' ? `/login?next=${encodeURIComponent(target)}` : '/login'
}
