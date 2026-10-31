import React, { useEffect, useLayoutEffect, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import QRCode from 'qrcode'
import { useI18n } from '../../lib/i18n'
import { copyTextToClipboard, triggerDownload } from '../../lib/clipboard'
import { logger } from '../../lib/logger'
import { Button } from '../ui'

type UrlShareControlProps = {
  path: string
  downloadName: string
  mode?: 'inline' | 'popover'
  /** What to show as the visible link text. Defaults to 'path' (e.g. /slides/1) — use 'full' for landing URLs where a bare "/" looks meaningless. */
  displayUrl?: 'path' | 'full'
}

type CopyStatus = 'idle' | 'copied' | 'failed'

// One shareable route (the current origin, or the canonical public origin).
type ShareRoute = {
  key: 'current' | 'public'
  label: string
  // Anchor href: a relative path for the current route (so the link opens on
  // whichever host the operator is on) or an absolute URL for the public route.
  href: string
  displayText: string
  // Full URL used for copy + QR (always absolute).
  url: string
}

function buildFullUrl(path: string): string {
  if (typeof window === 'undefined') return path
  return `${window.location.origin}${path}`
}

// The canonical public ("non-direct") URL for this path, or null when there is
// nothing extra to offer. Baked in from WSI_PUBLIC_BASE_URL at build time (see
// VITE_PUBLIC_BASE_URL in vite-env.d.ts / infra/install.sh). Returns null when
// the value is unset/blank/unparseable, or when the operator is already on the
// public origin — so the second URL only appears on a direct/local route (LAN
// host, bare IP, .local) where the current-origin URL wouldn't work off-LAN.
// Defensive parse (try/catch) mirrors src/lib/lanProbe.ts.
function buildPublicUrl(path: string): string | null {
  if (typeof window === 'undefined') return null
  const raw = import.meta.env.VITE_PUBLIC_BASE_URL
  if (!raw) return null
  let publicOrigin: string
  try {
    publicOrigin = new URL(raw).origin
  } catch {
    return null
  }
  if (publicOrigin === window.location.origin) return null
  return `${publicOrigin}${path}`
}

function QrPanel({ url, downloadName }: { url: string; downloadName: string }) {
  const { t } = useI18n()
  const canvasRef = useRef<HTMLCanvasElement | null>(null)
  const [error, setError] = useState(false)

  useEffect(() => {
    const el = canvasRef.current
    if (!el) return
    let cancelled = false
    setError(false)
    QRCode.toCanvas(el, url, { width: 192, margin: 2 }).catch((err) => {
      if (cancelled) return
      logger.error('QR generation failed', err)
      setError(true)
    })
    return () => { cancelled = true }
  }, [url])

  function download() {
    const el = canvasRef.current
    if (!el) return
    triggerDownload(el.toDataURL('image/png'), `${downloadName}-qr.png`)
  }

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'flex-start', gap: 6, padding: 8, background: 'var(--color-surface)', border: '1px solid var(--color-border)', borderRadius: 6 }}>
      <canvas ref={canvasRef} style={{ display: 'block', width: 192, height: 192, background: '#fff' }} />
      <div style={{ fontFamily: 'monospace', fontSize: 11, wordBreak: 'break-all', maxWidth: 192, color: 'var(--color-text-muted)' }}>{url}</div>
      {error ? (
        <div style={{ fontSize: 11, color: 'var(--color-danger-fg, #b91c1c)' }}>{t('urlShare.qrFailed')}</div>
      ) : (
        <Button type="button" size="sm" variant="secondary" onClick={download}>{t('urlShare.downloadQr')}</Button>
      )}
    </div>
  )
}

function CopyButton({ url }: { url: string }) {
  const { t } = useI18n()
  const [status, setStatus] = useState<CopyStatus>('idle')
  // Track the reset timer so it can be cleared: without this, closing the share
  // popover within the window would fire setState on an unmounted component,
  // and two rapid clicks let the first timer flip the second click's "copied"
  // label back to idle early. Mirrors SyncPresenterSidebar's copy button.
  const resetTimer = useRef<ReturnType<typeof setTimeout> | null>(null)
  useEffect(() => () => {
    if (resetTimer.current !== null) clearTimeout(resetTimer.current)
  }, [])
  function scheduleReset(ms: number) {
    if (resetTimer.current !== null) clearTimeout(resetTimer.current)
    resetTimer.current = setTimeout(() => {
      resetTimer.current = null
      setStatus('idle')
    }, ms)
  }
  async function copy() {
    try {
      await copyTextToClipboard(url)
      setStatus('copied')
      scheduleReset(1500)
    } catch (err) {
      logger.error('copy URL failed', err)
      setStatus('failed')
      scheduleReset(2000)
    }
  }
  const label = status === 'copied'
    ? t('urlShare.copied')
    : status === 'failed'
      ? t('urlShare.copyFailed')
      : t('urlShare.copy')
  return <Button type="button" size="sm" variant="secondary" onClick={copy}>{label}</Button>
}

// One route rendered inline: label + link + copy + a per-row QR toggle.
function InlineRouteRow({ route, downloadName }: { route: ShareRoute; downloadName: string }) {
  const { t } = useI18n()
  const [qrOpen, setQrOpen] = useState(false)
  return (
    <div style={{ display: 'inline-flex', flexDirection: 'column', alignItems: 'flex-start', gap: 6 }}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 6, fontSize: 12, flexWrap: 'wrap' }}>
        <span style={{ opacity: 0.75 }}>{route.label}</span>
        <a href={route.href} target="_blank" rel="noopener noreferrer" style={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>{route.displayText}</a>
        <CopyButton url={route.url} />
        <Button type="button" size="sm" variant="secondary" onClick={() => setQrOpen((v) => !v)}>
          {qrOpen ? t('urlShare.hideQr') : t('urlShare.showQr')}
        </Button>
      </span>
      {qrOpen && <QrPanel url={route.url} downloadName={downloadName} />}
    </div>
  )
}

function InlineRow({ routes, downloadName, hint }: { routes: ShareRoute[]; downloadName: string; hint?: string }) {
  return (
    <div style={{ display: 'inline-flex', flexDirection: 'column', alignItems: 'flex-start', gap: 8 }}>
      {routes.map((r) => (
        <InlineRouteRow key={r.key} route={r} downloadName={downloadName} />
      ))}
      {hint && <span style={{ fontSize: 11, color: 'var(--color-text-muted)', maxWidth: 320 }}>{hint}</span>}
    </div>
  )
}

// One route rendered inside the popover: label + link, then a copy button and a
// per-row QR toggle. The QR is hidden by default (mirrors InlineRouteRow) so the
// popover stays compact — two routes each showing a 192px QR would overflow
// the viewport.
function PopoverRouteRow({ route, downloadName, first }: { route: ShareRoute; downloadName: string; first: boolean }) {
  const { t } = useI18n()
  const [qrOpen, setQrOpen] = useState(false)
  return (
    <div
      style={{
        display: 'flex',
        flexDirection: 'column',
        gap: 8,
        ...(first ? {} : { borderTop: '1px solid var(--color-border)', paddingTop: 8 }),
      }}
    >
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, flexWrap: 'wrap' }}>
        <span style={{ opacity: 0.75 }}>{route.label}</span>
        <a href={route.href} target="_blank" rel="noopener noreferrer" style={{ fontFamily: 'monospace', wordBreak: 'break-all' }}>{route.displayText}</a>
      </div>
      <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'center' }}>
        <CopyButton url={route.url} />
        <Button type="button" size="sm" variant="secondary" onClick={() => setQrOpen((v) => !v)}>
          {qrOpen ? t('urlShare.hideQr') : t('urlShare.showQr')}
        </Button>
      </div>
      {qrOpen && <QrPanel url={route.url} downloadName={downloadName} />}
    </div>
  )
}

const POPOVER_WIDTH = 240

function PopoverShare({ routes, downloadName, hint }: { routes: ShareRoute[]; downloadName: string; hint?: string }) {
  const { t } = useI18n()
  const [open, setOpen] = useState(false)
  const triggerRef = useRef<HTMLButtonElement | null>(null)
  const popoverRef = useRef<HTMLDivElement | null>(null)
  const [coords, setCoords] = useState<{ top: number; left: number } | null>(null)

  useLayoutEffect(() => {
    if (!open) { setCoords(null); return }
    // Estimated popover height used only before the popover has mounted
    // and can be measured. The actual height is recomputed in the rAF
    // pass below.
    const ESTIMATED_HEIGHT = 360

    const reposition = () => {
      const trigger = triggerRef.current
      if (!trigger) return
      const triggerRect = trigger.getBoundingClientRect()
      const measuredHeight = popoverRef.current?.getBoundingClientRect().height
      const height = measuredHeight && measuredHeight > 0 ? measuredHeight : ESTIMATED_HEIGHT
      const margin = 8
      const gap = 4
      const spaceBelow = window.innerHeight - triggerRect.bottom - margin
      const spaceAbove = triggerRect.top - margin
      // Place below the trigger when there's room; otherwise flip above
      // — preferring whichever side has more space when neither fits in
      // full. This keeps the share popover usable when the trigger is
      // near the bottom of the viewport (e.g. last rows of the slide
      // list).
      let top: number
      if (height + gap <= spaceBelow || spaceBelow >= spaceAbove) {
        top = triggerRect.bottom + gap
      } else {
        top = triggerRect.top - height - gap
      }
      // Final clamp so the popover never sits outside the viewport even
      // when neither side has enough room.
      const maxTop = window.innerHeight - height - margin
      if (top > maxTop) top = Math.max(margin, maxTop)
      if (top < margin) top = margin

      // Right-align the popover to the trigger when there's space;
      // otherwise clamp inside the viewport.
      let left = triggerRect.right - POPOVER_WIDTH
      const maxLeft = window.innerWidth - POPOVER_WIDTH - margin
      if (left > maxLeft) left = maxLeft
      if (left < margin) left = margin

      setCoords((prev) =>
        prev && prev.top === top && prev.left === left ? prev : { top, left },
      )
    }

    reposition()
    // Re-measure once the popover has mounted so the vertical-clamp logic above
    // sees its real height instead of the estimate. The popover is on-screen by
    // the time this rAF runs, so also start observing its size: toggling a QR
    // panel open changes the height after mount, and the scroll/resize listeners
    // below don't fire for content changes — without this the popover could grow
    // off the bottom of the viewport when a QR is revealed near the screen edge.
    let resizeObserver: ResizeObserver | null = null
    const raf = requestAnimationFrame(() => {
      reposition()
      const el = popoverRef.current
      if (el && typeof ResizeObserver !== 'undefined') {
        resizeObserver = new ResizeObserver(() => reposition())
        resizeObserver.observe(el)
      }
    })
    // Reposition on scroll/resize rather than closing the popover —
    // a long slide list otherwise dismisses the popover as soon as
    // the user scrolls to compare URLs.
    window.addEventListener('scroll', reposition, true)
    window.addEventListener('resize', reposition)
    return () => {
      cancelAnimationFrame(raf)
      resizeObserver?.disconnect()
      window.removeEventListener('scroll', reposition, true)
      window.removeEventListener('resize', reposition)
    }
  }, [open])

  useEffect(() => {
    if (!open) return
    function onMouseDown(e: MouseEvent) {
      const target = e.target
      if (!(target instanceof Node)) return
      if (triggerRef.current && triggerRef.current.contains(target)) return
      if (popoverRef.current && popoverRef.current.contains(target)) return
      setOpen(false)
    }
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') setOpen(false)
    }
    document.addEventListener('mousedown', onMouseDown)
    document.addEventListener('keydown', onKey)
    return () => {
      document.removeEventListener('mousedown', onMouseDown)
      document.removeEventListener('keydown', onKey)
    }
  }, [open])

  return (
    <>
      <Button ref={triggerRef} type="button" size="sm" variant="secondary" onClick={() => setOpen((v) => !v)}>
        {t('common.share')}
      </Button>
      {open && coords && createPortal(
        <div
          ref={popoverRef}
          className="overscroll-panel"
          role="dialog"
          style={{
            position: 'fixed',
            top: coords.top,
            left: coords.left,
            width: POPOVER_WIDTH,
            zIndex: 1000,
            background: 'var(--color-surface)',
            border: '1px solid var(--color-border)',
            borderRadius: 6,
            boxShadow: 'var(--shadow-md, 0 4px 12px rgba(0,0,0,0.12))',
            padding: 10,
            display: 'flex',
            flexDirection: 'column',
            gap: 8,
            // Never taller than the viewport: with QR panels expanded the
            // content scrolls inside the popover instead of spilling off-screen.
            maxHeight: 'calc(100vh - 16px)',
            overflowY: 'auto',
          }}
        >
          {routes.map((r, i) => (
            <PopoverRouteRow key={r.key} route={r} downloadName={downloadName} first={i === 0} />
          ))}
          {hint && <div style={{ fontSize: 11, color: 'var(--color-text-muted)' }}>{hint}</div>}
          <div style={{ display: 'flex' }}>
            <Button type="button" size="sm" variant="secondary" onClick={() => setOpen(false)} style={{ marginLeft: 'auto' }}>{t('common.close')}</Button>
          </div>
        </div>,
        document.body,
      )}
    </>
  )
}

export default function UrlShareControl({ path, downloadName, mode = 'inline', displayUrl = 'path' }: UrlShareControlProps) {
  const { t } = useI18n()
  const fullUrl = buildFullUrl(path)
  const publicUrl = buildPublicUrl(path)
  const hasPublic = publicUrl !== null

  // With two routes each row gets a distinguishing label; with one route keep
  // the plain "URL:" label so the common case (public host, or the stock build
  // with no public base URL) is unchanged.
  const routes: ShareRoute[] = [
    {
      key: 'current',
      label: hasPublic ? t('urlShare.routeDirect') : t('urlShare.urlLabel'),
      href: path,
      displayText: displayUrl === 'full' ? fullUrl : path,
      url: fullUrl,
    },
  ]
  if (hasPublic && publicUrl) {
    routes.push({
      key: 'public',
      label: t('urlShare.routePublic'),
      // A bare path is meaningless for the other host, so always show/open the
      // full public URL.
      href: publicUrl,
      displayText: publicUrl,
      url: publicUrl,
    })
  }
  const hint = hasPublic ? t('urlShare.publicRouteHint') : undefined

  if (mode === 'popover') {
    return <PopoverShare routes={routes} downloadName={downloadName} hint={hint} />
  }
  return <InlineRow routes={routes} downloadName={downloadName} hint={hint} />
}
