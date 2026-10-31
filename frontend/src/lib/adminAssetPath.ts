// The admin bearer leaves the page here, so this names exactly what may
// receive it: a tour attachment, /assets/tours/<tour>/<file> on this origin.
// The path is normalised the way the browser will before it is judged —
// "/assets/../api/admin/slides/12/download" starts with "/assets/" but is
// requested as /api/admin/..., where a step description written by the lowest
// staff role would make an admin's editor fetch (and buffer) a whole slide with
// the admin's own credential. Returns the normalised path to request, or null
// for anything else (which then loads as an ordinary <img src>).
export function adminAssetPath(src: string): string | null {
  if (!src.startsWith('/assets/') || src.startsWith('//')) return null
  let url: URL
  try {
    url = new URL(src, window.location.origin)
  } catch {
    return null
  }
  if (url.origin !== window.location.origin) return null
  if (!url.pathname.startsWith('/assets/tours/')) return null
  return url.pathname + url.search
}
