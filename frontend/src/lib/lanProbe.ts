// QuickConnect-style LAN-direct redirect probe.
//
// When the app is reachable both via a public URL (e.g. through a Cloudflare
// Tunnel) and directly on the LAN under a separate hostname, this probe lets a
// single bookmarked URL transparently switch same-VLAN clients onto the
// LAN-direct route, while everyone else stays on the public URL. See
// docs/operations.md -> "Ingress paths (A / B / C)".
//
// Disabled by default: it only runs when VITE_LAN_HOST is set at build time
// (e.g. VITE_LAN_HOST=lan.wsi.example.org). Unset -> no-op, so the stock build
// is unaffected and generic deployments need no configuration.
//
// How it works: on first load it issues a no-cors HEAD fetch() for
// https://<VITE_LAN_HOST>/favicon.ico. The request resolving at all proves the
// LAN host is reachable AND completed a TLS handshake against a publicly-valid
// certificate for that hostname (the handshake is the authenticity proof — the
// opaque body is never read), so it redirects there. A network/TLS/DNS failure
// — or the abort timeout below — makes an attempt fail; on the fast path a few
// quick retries follow (see PROBE_FAST_ATTEMPTS) so a single slow cold
// connection doesn't strand the session, and only once every attempt has
// failed does the client stay on the current URL and continue via the public
// route.
//
// Why fetch() + AbortController and not <img>: an off-LAN client cannot reach
// the LAN host, so the request hangs until the OS TCP timeout (10+ seconds). An
// <img> load drives the browser tab's loading spinner for that whole stretch,
// and setting img.src back to '' does not reliably abort an in-flight image
// request, so the spinner would keep turning long after the probe had logically given
// up. A programmatic fetch() does not drive the tab spinner, and
// AbortController.abort() tears the connection down deterministically at the
// timeout, so an unreachable LAN host costs nothing visible.
//
// Why no-cors: a reachability probe needs none of the response, so an opaque
// response is fine and it sidesteps CORS preflight friction. favicon.ico is
// exempt from the app's auth gate (BYPASS_EXACT in
// services/app/wsi_app/app_auth.py — full-path equality, not a prefix) so the
// probe is answered even before the user has logged in.
//
// Why HEAD: the icon is a few hundred kB and the probe is uncached (see
// probeOnce), so a GET would download all of it on every session. HEAD (allowed
// in no-cors mode) moves no body: the app answers HEAD on /favicon.ico with
// GET's status and headers but no body (304 on a matching If-None-Match).
// Any response from the LAN host is all the probe asks for.
//
// CSP note: the cross-origin fetch is governed by the site's `connect-src`
// directive, so a deployment that enables this probe must add the LAN host to
// `connect-src` in its nginx CSP (see docs/operations.md). The stock build
// leaves VITE_LAN_HOST unset, so the default CSP needs no such entry.

import { STORAGE_KEYS } from './storageKeys'

const PROBE_TIMEOUT_MS = 500

// Number of reachability attempts on the fast (non-first) path before the
// probe gives up for the session. A single 500 ms attempt occasionally loses a
// race it should win: a cold TLS handshake or mDNS/DNS resolution to the LAN
// host can briefly exceed 500 ms even on a healthy same-VLAN client. Because
// the probe runs at most once per session (guarded below), a single attempt
// with no retry would let that one slow attempt pin the entire session to the
// public URL — the user would have to open a new tab to get another chance,
// which reads as "the LAN redirect only works sometimes". A couple of
// immediate retries turn a
// transient slow first connection into a successful redirect (the retry reuses
// the now-warm DNS/TLS state) while still costing an off-LAN client nothing
// visible: a fetch() drives no tab loading spinner and each attempt still
// aborts at PROBE_TIMEOUT_MS, so the worst case is 3 x 500 ms of silent,
// spinner-free waiting before the client settles on the public route.
const PROBE_FAST_ATTEMPTS = 3

// The very first probe on a device can collide with the browser's "allow
// local network access" permission prompt. Modern browsers gate access to
// local/private-network resources behind such a prompt — Chrome's Local
// Network Access permission (the successor to Private Network Access) and the
// local-network access prompts on iOS/iPadOS/macOS — so this is not specific
// to any one OS or vendor. On an intranet client the LAN host resolves onto
// the local network, so the first favicon fetch triggers that prompt and the
// request is held pending while it is on screen. A 500 ms abort fires long
// before the user can read the dialog and tap "Allow", so the first-ever
// probe always loses the race: the page the dialog appeared on (typically the
// top page, the first one loaded) aborts and never redirects, while every
// later page — probed after permission has already been granted — redirects
// in well under 500 ms. The result users see is exactly that asymmetry: later
// pages hand off to the LAN host but the top page stays on the public URL.
// Give the first-ever probe a generous window to let the user answer the
// prompt; a fetch() drives no tab loading spinner (see the long note above),
// so an off-LAN client that waits this window out pays nothing visible. Every
// later session uses the fast timeout, so off-LAN clients give up promptly
// once past the first load.
//
// The window is configurable at build time via VITE_LAN_PROBE_TIMEOUT_MS
// (operators set WSI_LAN_PROBE_TIMEOUT_MS; see docs/operations.md). A larger
// value buys more time to answer the permission prompt on the first load.
const DEFAULT_FIRST_PROBE_TIMEOUT_MS = 12000

// Resolve the first-probe window from VITE_LAN_PROBE_TIMEOUT_MS, falling back
// to the default for an unset / empty / non-positive / unparseable value so a
// misconfigured build can never disable the wait or wait forever.
function resolveFirstProbeTimeoutMs(): number {
  const raw = import.meta.env.VITE_LAN_PROBE_TIMEOUT_MS
  if (!raw) return DEFAULT_FIRST_PROBE_TIMEOUT_MS
  const parsed = Number.parseInt(raw, 10)
  return Number.isFinite(parsed) && parsed > 0 ? parsed : DEFAULT_FIRST_PROBE_TIMEOUT_MS
}
const FIRST_PROBE_TIMEOUT_MS = resolveFirstProbeTimeoutMs()

// Per-session guard: probe at most once per browser session.
const PROBE_SESSION_FLAG = 'wsi-lan-probed'
// Per-device guard: records that the browser's local-network permission prompt
// has already had its chance on this origin, so only the first-ever probe waits
// the long window above. Persisted in localStorage because the permission
// decision itself is per-device and survives sessions.
const PROBE_DEVICE_FLAG = 'wsi-lan-probed-once'

// A raw IP literal in the address bar (IPv4 dotted-quad or any IPv6 form)
// means the operator reached the server directly — the documented
// break-glass path used when the LAN hostname or its certificate is broken.
// Treat that as a deliberate choice and do not bounce the client to the
// hostname; otherwise a working hostname would defeat the very access the
// operator chose the IP to obtain.
function isIpLiteralHost(host: string): boolean {
  if (host.includes(':')) return true // IPv6 literal
  return /^\d{1,3}(\.\d{1,3}){3}$/.test(host) // IPv4 dotted-quad
}

export function runLanProbe(): void {
  const lanHost = import.meta.env.VITE_LAN_HOST
  if (!lanHost) return // feature disabled (default build)

  if (typeof window === 'undefined') return

  // Already on the LAN host: nothing to do (also prevents a redirect loop).
  if (window.location.hostname === lanHost) return

  // Direct IP access is a deliberate break-glass choice — leave it alone.
  if (isIpLiteralHost(window.location.hostname)) return

  // Need fetch + AbortController for a cancellable probe; bail (no redirect)
  // on the rare browser without them rather than risk an uncancellable hang.
  if (typeof fetch !== 'function' || typeof AbortController === 'undefined') return

  // Probe at most once per browser session. sessionStorage (not localStorage)
  // is deliberate: a laptop carried off the LAN must re-evaluate on its next
  // session instead of being pinned to a now-unreachable LAN host.
  try {
    if (window.sessionStorage.getItem(PROBE_SESSION_FLAG)) return
    window.sessionStorage.setItem(PROBE_SESSION_FLAG, '1')
  } catch {
    // sessionStorage unavailable (private mode / quota) — skip silently.
    return
  }

  // First-ever probe on this device gets the long window so it can outlast the
  // browser's local-network permission prompt; afterwards mark the device so later sessions stay
  // fast. localStorage may be unavailable (private mode / quota) — treat that
  // as "not first" and keep the fast path rather than risk a long wait on
  // every load.
  let firstEver = false
  try {
    firstEver = !window.localStorage.getItem(PROBE_DEVICE_FLAG)
  } catch {
    firstEver = false
  }
  const markDeviceProbed = () => {
    try {
      window.localStorage.setItem(PROBE_DEVICE_FLAG, '1')
    } catch {
      // localStorage unavailable — nothing to persist, fall back to fast path.
    }
  }
  // Hand the client off to the LAN-direct route. Strips the referrer on this
  // internal public->LAN hand-off. The two ingress routes are the same app
  // under different hostnames, so without this the LAN host's document.referrer
  // would be the public hostname for the whole document lifetime, and the
  // analytics beacon (which reads document.referrer) would log every
  // LAN-switched visit as referred by the app's own public host instead of as
  // direct traffic. A no-referrer policy set synchronously before the
  // navigation governs the referrer exposed to the destination, so the LAN page
  // loads with an empty document.referrer. The genuine inbound referrer of the
  // original public load (e.g. a search engine) is on the public host's page
  // only and is not carried across the route switch — that is acceptable: the
  // route a same-VLAN client lands on is an internal detail, not a traffic
  // source.
  const redirectToLan = () => {
    // Not for a visitor who has meanwhile entered the dashboard (the probe can
    // resolve ~12 s after the page loaded): the LAN origin has its own
    // sessionStorage, so the staff token and any upload or restore under way
    // would be left behind on this one. Staff reach the LAN route on a later
    // session instead; the probe is once per session anyway.
    try {
      if (window.location.pathname.startsWith('/admin') || window.sessionStorage.getItem(STORAGE_KEYS.adminToken)) return
    } catch {
      // sessionStorage unreadable: nothing to protect.
    }
    const noReferrer = document.createElement('meta')
    noReferrer.name = 'referrer'
    noReferrer.content = 'no-referrer'
    document.head.appendChild(noReferrer)
    // Read the current location at redirect time, not at runLanProbe() entry:
    // the probe can resolve up to ~12s later (plus retries), and if the user
    // navigated within the SPA during that window, a path captured at entry
    // would location.replace() them back to a stale location on the LAN host,
    // losing their in-app navigation.
    const { pathname, search, hash } = window.location
    window.location.replace(`https://${lanHost}${pathname}${search}${hash}`)
  }

  // A single no-cors reachability probe with its own bounded, abortable
  // timeout. Resolves true when the LAN host answered at all (any response —
  // see the no-cors note above), false on a network / TLS / DNS failure or the
  // abort timeout. Cache-bust + no-store so a cached response can't satisfy
  // the probe after the LAN host has gone away mid-session.
  const probeOnce = (timeoutMs: number): Promise<boolean> => {
    const controller = new AbortController()
    const timer = window.setTimeout(() => controller.abort(), timeoutMs)
    return fetch(`https://${lanHost}/favicon.ico?t=${Date.now()}`, {
      method: 'HEAD',
      mode: 'no-cors',
      cache: 'no-store',
      signal: controller.signal,
    }).then(
      () => { window.clearTimeout(timer); return true },
      () => { window.clearTimeout(timer); return false },
    )
  }

  if (firstEver) {
    // First-ever probe: a single long window whose only job is to outlast the
    // browser's local-network permission prompt (see FIRST_PROBE_TIMEOUT_MS).
    // Retrying wouldn't help — the wait is for the user to answer a dialog, not
    // a latency race — so keep it to one attempt. Mark the device probed either
    // way so every later session takes the fast path.
    probeOnce(FIRST_PROBE_TIMEOUT_MS).then((reachable) => {
      markDeviceProbed()
      if (reachable) redirectToLan()
    })
    return
  }

  // Fast path (the device flag is already set, or localStorage is unusable):
  // retry a few times before giving up for the session, so a transient slow
  // first connection doesn't strand the client on the public URL with no second
  // chance (see PROBE_FAST_ATTEMPTS). Attempts run strictly one at a time — the
  // next only starts once the previous resolved false — so there is never more
  // than one probe in flight, and the first success redirects immediately.
  // Once every attempt has failed (unreachable, off-LAN, or persistently slow)
  // the client simply stays on the current URL.
  let attemptsLeft = PROBE_FAST_ATTEMPTS
  const probeWithRetry = () => {
    probeOnce(PROBE_TIMEOUT_MS).then((reachable) => {
      if (reachable) {
        redirectToLan()
        return
      }
      attemptsLeft -= 1
      if (attemptsLeft > 0) probeWithRetry()
    })
  }
  probeWithRetry()
}
