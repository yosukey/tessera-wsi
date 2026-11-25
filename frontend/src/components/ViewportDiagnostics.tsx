import React, { useEffect, useState } from 'react'
import { useLocation } from 'react-router'
import { APP_INFO } from '../lib/appInfo'
import { safeSessionGetItem, safeSessionRemoveItem, safeSessionSetItem } from '../lib/safeStorage'
import { STORAGE_KEYS } from '../lib/storageKeys'
import { TOUR_EDITOR_STACK_QUERY, TOUR_VIEWER_STACK_QUERY } from '../lib/tourEditorLayout'
import {
  fixedBottom, matchesViewportQuery, measureViewportBox, VIEWPORT_CLIP_X_CLASS, VIEWPORT_STALE_CLASS,
} from '../lib/viewportBox'

// `?vpdebug=1` turns the panel on for this tab (it survives in-app navigation
// and reloads), `?vpdebug=0` turns it off. Nothing is shown otherwise.
export function viewportDiagnosticsEnabled(search: string): boolean {
  const flag = new URLSearchParams(search).get('vpdebug')
  if (flag === '1') safeSessionSetItem(STORAGE_KEYS.viewportDiagnostics, '1')
  else if (flag === '0') safeSessionRemoveItem(STORAGE_KEYS.viewportDiagnostics)
  return safeSessionGetItem(STORAGE_KEYS.viewportDiagnostics) === '1'
}

const QUERIES: Array<[string, string]> = [
  ['portrait', '(orientation: portrait)'],
  ['tourViewerStack', TOUR_VIEWER_STACK_QUERY],
  ['tourEditorStack', TOUR_EDITOR_STACK_QUERY],
  ['dashboardFluid', '(max-width: 1080px)'],
]

const r = Math.round

// Everything that decides whether the page fits the screen, read live.
export function viewportSnapshot(): string[] {
  const vv = window.visualViewport
  const de = document.documentElement
  const so = typeof screen !== 'undefined' ? screen.orientation : undefined
  const legacyOrientation = (window as unknown as { orientation?: number }).orientation
  const page = document.getElementById('root')?.firstElementChild?.getBoundingClientRect()
  const meta = document.querySelector<HTMLMetaElement>('meta[name="viewport"]')?.content ?? '(none)'
  return [
    `build ${APP_INFO.version}  ${location.pathname}`,
    `ua ${navigator.userAgent}`,
    `touchPoints ${navigator.maxTouchPoints}  dpr ${window.devicePixelRatio}`,
    `meta ${meta}`,
    `screen ${screen.width}x${screen.height}  ${so ? `${so.type} ${so.angle}°` : 'no screen.orientation'}  window.orientation ${legacyOrientation ?? '-'}`,
    `layout(inner) ${window.innerWidth}x${window.innerHeight}  html.client ${de.clientWidth}x${de.clientHeight}`,
    vv
      ? `visual ${r(vv.width)}x${r(vv.height)}  scale ${vv.scale.toFixed(3)}  offset ${r(vv.offsetLeft)},${r(vv.offsetTop)}`
      : 'visual (no visualViewport)',
    `doc.scroll ${de.scrollWidth}x${de.scrollHeight}  overflowX ${de.scrollWidth - window.innerWidth}  overflowY ${de.scrollHeight - window.innerHeight}  at ${r(window.scrollX)},${r(window.scrollY)}`,
    `page root ${page ? `${r(page.width)}x${r(page.height)} at ${r(page.left)},${r(page.top)}` : '-'}`,
    `media(css) ${QUERIES.map(([name, q]) => `${name}=${window.matchMedia(q).matches ? 1 : 0}`).join(' ')}`,
    (() => {
      const box = measureViewportBox()
      const fix = de.classList.contains(VIEWPORT_STALE_CLASS)
      return `stale layout viewport ${box.stale ? 'YES' : 'no'}  fix ${fix ? `ON ${de.style.getPropertyValue('--vp-width')}x${de.style.getPropertyValue('--vp-height')}${de.classList.contains(VIEWPORT_CLIP_X_CLASS) ? ' clip-x' : ''}` : 'off'}`
        + `\nmedia(measured) ${QUERIES.map(([name, q]) => `${name}=${matchesViewportQuery(q, box.width, box.height) ? 1 : 0}`).join(' ')}`
    })(),
  ]
}

// The viewport-related events as they arrive, with what the page saw at that
// moment: a rotation that the browser reports late, or not at all, shows here.
function useEventLog(enabled: boolean): string[] {
  const [log, setLog] = useState<string[]>([])
  useEffect(() => {
    if (!enabled) return
    const t0 = performance.now()
    const add = (type: string) => {
      const vv = window.visualViewport
      const line = `${((performance.now() - t0) / 1000).toFixed(2)}s ${type}: inner ${window.innerWidth}x${window.innerHeight}`
        + (vv ? ` vv ${r(vv.width)}x${r(vv.height)}@${vv.scale.toFixed(2)}` : '')
        + ` scrollW ${document.documentElement.scrollWidth}`
      setLog((prev) => [...prev.slice(-9), line])
    }
    const onResize = () => add('resize')
    const onOrientation = () => add('orientationchange')
    const onScreen = () => add('screen.orientation')
    const onVv = () => add('vv.resize')
    const so = typeof screen !== 'undefined' ? screen.orientation : undefined
    const ro = typeof ResizeObserver === 'function' ? new ResizeObserver(() => add('html resized')) : null
    window.addEventListener('resize', onResize)
    window.addEventListener('orientationchange', onOrientation)
    so?.addEventListener?.('change', onScreen)
    window.visualViewport?.addEventListener('resize', onVv)
    ro?.observe(document.documentElement)
    return () => {
      window.removeEventListener('resize', onResize)
      window.removeEventListener('orientationchange', onOrientation)
      so?.removeEventListener?.('change', onScreen)
      window.visualViewport?.removeEventListener('resize', onVv)
      ro?.disconnect()
    }
  }, [enabled])
  return log
}

/**
 * Viewport diagnostics panel, for finding out on a real device why a page
 * doesn't fit the screen (e.g. after rotating a tablet): the layout and
 * visual viewport sizes, the page zoom, how far the document overflows, the
 * layout media queries, the viewport meta, the build, and a log of the
 * viewport events as they arrived. A screenshot of it after the problem shows
 * is the report. Fixed and click-through, so it doesn't change the layout it
 * measures.
 */
export default function ViewportDiagnostics() {
  const location = useLocation()
  const enabled = viewportDiagnosticsEnabled(location.search)
  const [lines, setLines] = useState<string[]>([])
  const log = useEventLog(enabled)
  useEffect(() => {
    if (!enabled) return
    const update = () => setLines(viewportSnapshot())
    update()
    const id = window.setInterval(update, 250)
    return () => window.clearInterval(id)
  }, [enabled])
  if (!enabled) return null
  return (
    <div
      aria-hidden="true"
      data-testid="viewport-diagnostics"
      style={{
        // bottom: anchored to the measured viewport, so the panel stays on
        // screen while the layout viewport is stale — the case it reports.
        position: 'fixed', left: 4, bottom: fixedBottom(4), zIndex: 2147483647,
        maxWidth: 'min(440px, calc(var(--vp-width, 100%) - 8px))', maxHeight: 'calc(var(--vp-height, 100%) * 0.6)', overflow: 'hidden',
        padding: 6, borderRadius: 6, background: 'rgba(0, 0, 0, 0.78)', color: '#fff',
        font: '10px/1.35 ui-monospace, SFMono-Regular, Menlo, monospace',
        whiteSpace: 'pre-wrap', wordBreak: 'break-all', pointerEvents: 'none',
      }}
    >
      {lines.join('\n')}
      {log.length > 0 && `\n— events —\n${log.join('\n')}`}
    </div>
  )
}
