import { useEffect, useState } from 'react'
import { adminFetchImageBlobUrl } from '../lib/api'

// A blob URL for an image the dashboard previews (logo, header and seasonal
// backgrounds, favicon, tour assets), fetched with the admin bearer. A plain
// <img src> / CSS url() sends no Authorization header, so under the ldap /
// google_oauth gate an admin without an app-access cookie would get a 401 (and an
// admin-only route such as a disabled seasonal slot's image needs the bearer
// in any mode). "" until loaded, for an empty `url`, or when the fetch
// fails; the previous blob is revoked when `url` changes or on unmount.
export function useAdminBlobUrl(url: string): string {
  const [blobUrl, setBlobUrl] = useState('')
  useEffect(() => {
    setBlobUrl('')
    if (!url) return
    const ac = new AbortController()
    let cancelled = false
    let created = ''
    adminFetchImageBlobUrl(url, { signal: ac.signal })
      .then((u) => {
        if (cancelled) {
          if (u) URL.revokeObjectURL(u)
          return
        }
        created = u
        setBlobUrl(u)
      })
      .catch(() => { /* aborted or offline: no preview */ })
    return () => {
      cancelled = true
      ac.abort()
      if (created) URL.revokeObjectURL(created)
    }
  }, [url])
  return blobUrl
}

// The value for a CSS `background-image` showing `blobUrl` ("none" while it
// is not loaded).
export function cssImageUrl(blobUrl: string): string {
  return blobUrl ? `url("${blobUrl}")` : 'none'
}
