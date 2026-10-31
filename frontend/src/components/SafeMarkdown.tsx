import React, { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import ReactMarkdown from 'react-markdown'
import remarkGfm from 'remark-gfm'
import rehypeSanitize, { defaultSchema } from 'rehype-sanitize'
import { useI18n } from '../lib/i18n'
import { useDialogFocus } from '../hooks/useDialogFocus'
import { adminFetchImageBlobUrl } from '../lib/api'
import { adminAssetPath } from '../lib/adminAssetPath'
import styles from './SafeMarkdown.module.css'

// True while rendering the descendants of a markdown link. An image inside a
// link renders as a plain (non-zoomable) image so a click follows the link
// instead of ALSO popping the lightbox — otherwise `[![](img)](url)` would
// both open `url` in a new tab and show the full-screen zoom overlay.
const InLinkContext = createContext(false)

// Opens the lightbox for a zoomable image. Passed through context (rather than
// closed over in `components`) so the components map below can stay a
// module-level constant: react-markdown uses each entry as an element type, and
// a fresh arrow per render would remount every image on each keystroke in the
// tour editor's live preview.
const ZoomContext = createContext<(z: ZoomTarget) => void>(() => {})

// True when this markdown is previewed in the admin tour editor. There, a
// same-origin tour asset (/assets/...) is fetched with the admin bearer: under
// the ldap / google_oauth gate a plain <img src> only loads with an app-access
// cookie, which an admin may not have — or had, until it expired while the
// admin bearer stayed valid, so the image would show at first and then break.
const AdminImagesContext = createContext(false)

const safeSchema = {
  ...defaultSchema,
  tagNames: [
    'p', 'br', 'strong', 'em', 'ul', 'ol', 'li',
    'a', 'img', 'code', 'pre', 'blockquote',
    // Block structure: horizontal rules and GFM tables. No attributes are
    // whitelisted for these (see `attributes` below, which replaces the default
    // schema's), so the `align` attribute that GFM column alignment (:---:)
    // produces is dropped and every cell renders left-aligned. `style` stays
    // disallowed too. Presentation lives in SafeMarkdown.module.css.
    'hr', 'table', 'thead', 'tbody', 'tr', 'td', 'th',
  ],
  attributes: {
    a: ['href', 'title', 'target', 'rel'],
    img: ['src', 'alt', 'title', 'width', 'height'],
    // Restrict to `language-*` (the value rehype-sanitize's defaultSchema
    // allows) so authored markdown can't smuggle arbitrary classes onto <code>
    // and adopt app CSS for UI spoofing. ```js etc. still become language-js.
    code: [['className', /^language-./]],
  },
  protocols: {
    ...defaultSchema.protocols,
    href: ['http', 'https', 'mailto'],
    // No data: — `data:` URIs in <img> can hide arbitrary payloads (e.g. an
    // obscured tracker, or a multi-megabyte SVG/PNG that bloats the DB row
    // and slows every render). Tour-asset images are uploaded through the
    // /api/admin/tours/{id}/assets/upload pipeline (which validates content
    // and assigns an /assets/... URL), so legitimate authoring never needs
    // data: here.
    src: ['http', 'https'],
  },
  clobberPrefix: 'md-'
}

type Props = {
  markdown: string
  // Load same-origin /assets/ images with the admin bearer (admin editor only).
  adminImages?: boolean
}

// Always force at least these two `rel` tokens on every rendered link so an
// open-in-new-tab destination cannot reach back into this window via
// window.opener and cannot leak the source URL via Referer.
const REQUIRED_LINK_REL = ['noreferrer', 'noopener']

function mergeLinkRel(existing: string | undefined): string {
  const tokens = (existing || '')
    .split(/\s+/)
    .map((t) => t.trim().toLowerCase())
    .filter(Boolean)
  for (const required of REQUIRED_LINK_REL) {
    if (!tokens.includes(required)) tokens.push(required)
  }
  return tokens.join(' ')
}

type ZoomTarget = { src: string; alt: string }

// The src to render: `src` itself, or — in the admin editor, for a local asset
// — a blob URL fetched with the admin bearer. While that fetch is in flight
// the image has no src; if it fails the plain URL is used, so the image still
// loads whenever the browser has app access on its own.
function useMarkdownImageSrc(src: string): string | undefined {
  const adminImages = useContext(AdminImagesContext)
  const assetPath = adminImages ? adminAssetPath(src) : null
  const viaAdmin = assetPath !== null
  const [state, setState] = useState<{ src: string; url: string } | null>(null)
  useEffect(() => {
    setState(null)
    if (!viaAdmin) return
    const ac = new AbortController()
    let cancelled = false
    let created = ''
    adminFetchImageBlobUrl(assetPath, { signal: ac.signal })
      .then((u) => {
        if (cancelled) {
          if (u) URL.revokeObjectURL(u)
          return
        }
        created = u
        setState({ src, url: u || src })
      })
      .catch(() => { if (!cancelled) setState({ src, url: src }) })
    return () => {
      cancelled = true
      ac.abort()
      if (created) URL.revokeObjectURL(created)
    }
  }, [src, assetPath, viaAdmin])
  if (!viaAdmin) return src || undefined
  return state && state.src === src ? state.url : undefined
}

// A markdown image. Standalone images are zoomable (click / Enter / Space opens
// the lightbox); an image nested in a link renders plain so the click follows
// the link instead of also opening the overlay.
function MarkdownImage(props: React.ImgHTMLAttributes<HTMLImageElement>) {
  const { t } = useI18n()
  const inLink = useContext(InLinkContext)
  const onZoom = useContext(ZoomContext)
  const rawSrc = typeof props.src === 'string' ? props.src : ''
  const src = useMarkdownImageSrc(rawSrc)
  const alt = typeof props.alt === 'string' ? props.alt : ''

  if (inLink) {
    return <img {...props} src={src} style={{ maxWidth: '100%', height: 'auto' }} />
  }

  const openZoom = () => { if (src) onZoom({ src, alt }) }
  return (
    <img
      {...props}
      src={src}
      onClick={openZoom}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); openZoom() }
      }}
      role="button"
      tabIndex={0}
      title={typeof props.title === 'string' && props.title ? props.title : t('markdown.imageZoomHint')}
      style={{ maxWidth: '100%', height: 'auto', cursor: 'zoom-in' }}
    />
  )
}

// The full-viewport overlay a zoomable image opens. Mounted only while open,
// so its effects bracket exactly one opening: Escape closes it, background
// scroll is locked (a wheel / trackpad gesture over the dark backdrop must not
// scroll the page beneath), focus moves in and is kept there, and it returns
// to the image that opened it on close.
function ImageLightbox({ zoom, onClose }: { zoom: ZoomTarget; onClose: () => void }) {
  const { t } = useI18n()
  const dialogRef = useRef<HTMLDivElement | null>(null)
  useDialogFocus(dialogRef)

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => { if (e.key === 'Escape') onClose() }
    document.addEventListener('keydown', onKey)
    const prevOverflow = document.body.style.overflow
    document.body.style.overflow = 'hidden'
    return () => {
      document.removeEventListener('keydown', onKey)
      document.body.style.overflow = prevOverflow
    }
  }, [onClose])

  return createPortal(
    <div
      ref={dialogRef}
      className="overscroll-panel"
      tabIndex={-1}
      role="dialog"
      aria-modal="true"
      aria-label={zoom.alt || t('markdown.imageZoomHint')}
      onClick={onClose}
      style={{
        position: 'fixed',
        inset: 0,
        zIndex: 'calc(var(--z-modal, 1000) + 20)' as unknown as number,
        background: 'rgba(0, 0, 0, 0.85)',
        display: 'flex',
        alignItems: 'center',
        justifyContent: 'center',
        padding: 24,
        cursor: 'zoom-out',
        overflow: 'auto',
      }}
    >
      <button
        type="button"
        onClick={(e) => { e.stopPropagation(); onClose() }}
        aria-label={t('common.close')}
        style={{
          position: 'fixed',
          top: 12,
          right: 12,
          zIndex: 1,
          background: 'rgba(255, 255, 255, 0.92)',
          border: '1px solid var(--color-border, #ccc)',
          borderRadius: 'var(--radius-md, 6px)',
          padding: '4px 10px',
          fontSize: 12,
          fontWeight: 600,
          cursor: 'pointer',
        }}
      >
        {t('markdown.imageZoomClose')}
      </button>
      <img
        src={zoom.src}
        alt={zoom.alt}
        // Stop the backdrop's close handler so a click that lands on the
        // image itself doesn't dismiss the lightbox.
        onClick={(e) => e.stopPropagation()}
        style={{
          display: 'block',
          maxWidth: '100%',
          maxHeight: '100%',
          objectFit: 'contain',
          boxShadow: '0 4px 24px rgba(0, 0, 0, 0.6)',
          cursor: 'default',
        }}
      />
    </div>,
    document.body,
  )
}

const MARKDOWN_COMPONENTS: React.ComponentProps<typeof ReactMarkdown>['components'] = {
  // `node` is the hast AST node react-markdown threads through; it
  // isn't rendered here, so destructure it out before forwarding the
  // rest as DOM attrs (otherwise React warns about an unknown prop).
  a: ({ node: _node, rel, ...props }) => (
    <InLinkContext.Provider value={true}>
      <a {...props} target="_blank" rel={mergeLinkRel(rel)} />
    </InLinkContext.Provider>
  ),
  img: ({ node: _node, ...props }) => <MarkdownImage {...props} />,
}

function SafeMarkdown({ markdown, adminImages = false }: Props) {
  // Source of the image currently shown enlarged in the lightbox, or null when
  // closed. Embedded images render inline at their natural size, scaled down to
  // the container width; clicking one opens a full-viewport overlay so fine
  // detail is legible without leaving the page.
  const [zoom, setZoom] = useState<ZoomTarget | null>(null)
  const closeZoom = useCallback(() => setZoom(null), [])

  // Memoise the parsed markdown on the source string alone (setZoom is stable),
  // so opening/closing the lightbox — which re-renders this component via the
  // `zoom` state — doesn't re-run react-markdown's remark+rehype+sanitize
  // pipeline over an unchanged document.
  const rendered = useMemo(() => (
    <ReactMarkdown
      remarkPlugins={[remarkGfm]}
      rehypePlugins={[[rehypeSanitize, safeSchema]]}
      components={MARKDOWN_COMPONENTS}
    >
      {markdown || ''}
    </ReactMarkdown>
  ), [markdown])

  return (
    <>
      <ZoomContext.Provider value={setZoom}>
        <AdminImagesContext.Provider value={adminImages}>
          <div className={styles.prose}>
            {rendered}
          </div>
        </AdminImagesContext.Provider>
      </ZoomContext.Provider>
      {zoom && <ImageLightbox zoom={zoom} onClose={closeZoom} />}
    </>
  )
}

// Memoised on the `markdown` prop so a parent re-render that doesn't change the
// source (e.g. dragging an image-adjust slider in the tour editor, which
// re-renders the whole step list on every input event) doesn't re-parse every
// step's description through react-markdown.
export default React.memo(SafeMarkdown)
