import React, { useEffect, useState } from 'react'
import {
  IpRule,
  NetworkSettings,
  adminGetMyIp,
  adminGetNetworkSettings,
  adminUpdateNetworkSettings,
  isAbortError,
  isApiCodeError,
} from '../../lib/api'
import { useI18n } from '../../lib/i18n'
import { apiErrorMessage } from '../../lib/admin/apiError'
import { firstMatchingIpRule, rulesAllowIp } from '../../lib/admin/ipFilter'
import { logger } from '../../lib/logger'
import { useInFlight } from '../../hooks/useInFlight'
import { LockFieldset } from '../ui'
import UrlShareControl from './UrlShareControl'
import {
  DIRTY_SAVE_STYLE,
  FIELD_LABEL_STYLE,
  SECTION_STYLE,
  SECTION_TITLE_STYLE,
  SaveResult,
  UnsavedChangesChip,
} from './networkAuthUi'

// Mirrors the backend's IpRuleIn.ip canonicalization in services/app/wsi_app/schemas.py
// so the front-end's duplicate check considers `192.168.1.1` and
// `192.168.001.001/32` to be the same rule. For IPv4 CIDRs the host bits
// are also masked off so `192.168.1.5/24` collapses to `192.168.1.0/24`,
// matching `ipaddress.ip_network(strict=False)` server-side. IPv6 is only
// lowercased — full IPv6 canonicalization is non-trivial and the server
// dedupes anyway. Falls back to a trimmed string when the input isn't a
// valid IP / CIDR; the server still does the authoritative validation.
function canonicalIpRuleKey(raw: string): string {
  const v = raw.trim()
  if (!v) return ''
  try {
    if (v.includes('/')) {
      const [addr, maskStr] = v.split('/', 2)
      const prefix = parseInt(maskStr, 10)
      if (addr.includes(':')) {
        return `${addr.toLowerCase()}/${prefix}`
      }
      return `${ipv4NetworkAddr(addr, prefix)}/${prefix}`
    }
    // A bare address is equivalent to its host route, so canonicalize to the
    // /32 (IPv4) or /128 (IPv6) form — matching ip_network(strict=False)
    // server-side — so "192.168.1.1" and "192.168.1.1/32" dedupe together.
    // Without this the Add-rule duplicate check lets both forms through and
    // the server silently drops one on save.
    const bare = normalizeAddr(v)
    return bare.includes(':') ? `${bare}/128` : `${bare}/32`
  } catch {
    return v.toLowerCase()
  }
}

// The server's 422 code for a save its self-lockout guard refuses.
const IP_LOCKOUT_ERROR_CODE = 'adminDash.apiErr.ipFilterSelfLockout'

function normalizeAddr(addr: string): string {
  if (addr.includes(':')) return addr.toLowerCase()
  // IPv4: drop leading zeros from each octet so 192.168.001.001 == 192.168.1.1.
  return addr
    .split('.')
    .map((o) => String(parseInt(o, 10)))
    .join('.')
}

// Mask host bits off so 192.168.1.5/24 normalizes to 192.168.1.0/24,
// matching the backend's ipaddress.ip_network(strict=False). Uses
// arithmetic rather than 32-bit bitwise ops to avoid the sign-bit
// surprises of JS `&`/`>>>` on values >= 2**31.
function ipv4NetworkAddr(addr: string, prefix: number): string {
  if (!Number.isInteger(prefix) || prefix < 0 || prefix > 32) {
    throw new Error('invalid prefix')
  }
  const parts = addr.split('.').map((o) => parseInt(o, 10))
  if (parts.length !== 4 || parts.some((p) => !Number.isInteger(p) || p < 0 || p > 255)) {
    throw new Error('invalid ipv4')
  }
  const intAddr = parts[0] * 0x1000000 + parts[1] * 0x10000 + parts[2] * 0x100 + parts[3]
  const hostBits = 32 - prefix
  const block = Math.pow(2, hostBits)
  const netInt = Math.floor(intAddr / block) * block
  const a = Math.floor(netInt / 0x1000000) % 256
  const b = Math.floor(netInt / 0x10000) % 256
  const c = Math.floor(netInt / 0x100) % 256
  const d = netInt % 256
  return `${a}.${b}.${c}.${d}`
}

// JSON snapshot of the network-settings fields the Save button persists.
// mdns_active / analytics_hash_salt_available are derived server-side and
// excluded so the dirty indicator doesn't flash when the server reports
// status changes.
function networkSnapshot(n: NetworkSettings): string {
  return JSON.stringify({
    ip_filter_enabled: n.ip_filter_enabled,
    ip_filter_default: n.ip_filter_default,
    ip_rules: n.ip_rules,
    mdns_enabled: n.mdns_enabled,
    ga_measurement_id: n.ga_measurement_id,
    analytics_mode: n.analytics_mode,
    analytics_notice_toast_enabled: n.analytics_notice_toast_enabled,
  })
}

// The Network tab's network settings: the top-page URL, the IP filter, mDNS
// and analytics, saved together by their own Save button.
export default function AdminNetworkPanel({ onDirtyChange }: {
  // Whether the form has unsaved edits (AdminNetworkAuthSection guards them).
  onDirtyChange: (dirty: boolean) => void
}) {
  const { t } = useI18n()
  const [netSettings, setNetSettings] = useState<NetworkSettings | null>(null)
  // null = no load failure; otherwise the localized reason ('' when none).
  const [netLoadErr, setNetLoadErr] = useState<string | null>(null)
  const [netMsg, setNetMsg] = useState('')
  const [netMsgIsError, setNetMsgIsError] = useState(false)
  const saveRequest = useInFlight()
  const netSaving = saveRequest.busy
  const [netSavedSnapshot, setNetSavedSnapshot] = useState('')
  const [myIp, setMyIp] = useState('')
  const [myIpViaCfTunnel, setMyIpViaCfTunnel] = useState(false)
  const [newRuleIp, setNewRuleIp] = useState('')
  const [newRuleAction, setNewRuleAction] = useState<'allow' | 'deny'>('allow')
  const [newRuleNote, setNewRuleNote] = useState('')
  const [newRuleErr, setNewRuleErr] = useState('')
  const [netReloadKey, setNetReloadKey] = useState(0)

  useEffect(() => {
    const ctrl = new AbortController()
    setNetLoadErr(null)
    adminGetNetworkSettings({ signal: ctrl.signal })
      .then((s) => {
        setNetSettings(s)
        setNetSavedSnapshot(networkSnapshot(s))
      })
      .catch((err) => { if (!isAbortError(err)) setNetLoadErr(apiErrorMessage(err, t)) })
    adminGetMyIp({ signal: ctrl.signal })
      .then((r) => { setMyIp(r.ip); setMyIpViaCfTunnel(!!r.via_cloudflare_tunnel) })
      .catch((err) => { if (!isAbortError(err)) logger.error(err) })
    return () => ctrl.abort()
  }, [netReloadKey])

  // Dirty only after the initial GET has filled in the saved snapshot, so
  // pre-load state can't claim "unsaved changes".
  const netIsDirty = !!netSavedSnapshot && !!netSettings && networkSnapshot(netSettings) !== netSavedSnapshot
  useEffect(() => {
    onDirtyChange(netIsDirty)
  }, [netIsDirty, onDirtyChange])

  if (netLoadErr !== null && !netSettings) {
    return (
      <div role="alert" style={{ fontSize: 12, display: 'grid', gap: 6 }}>
        <div style={{ color: 'var(--color-danger-fg)' }}>{t('adminDash.netLoadFailed')}</div>
        {netLoadErr && <div>{netLoadErr}</div>}
        <div>
          <button type="button" onClick={() => setNetReloadKey((k) => k + 1)} style={{ fontSize: 12 }}>
            {t('adminDash.retry')}
          </button>
        </div>
      </div>
    )
  }

  if (!netSettings) {
    return <div style={{ fontSize: 12, opacity: 0.6 }}>{t('adminDash.loading')}</div>
  }

  const moveRule = (index: number, delta: -1 | 1) => {
    setNetSettings((s) => {
      if (!s) return s
      const j = index + delta
      if (j < 0 || j >= s.ip_rules.length) return s
      const next = s.ip_rules.slice()
      const tmp = next[index]
      next[index] = next[j]
      next[j] = tmp
      return { ...s, ip_rules: next }
    })
  }

  return (
    <>
      {/* Top page URL */}
      <div style={SECTION_STYLE}>
        <div style={SECTION_TITLE_STYLE}>
          {t('adminDash.topPageUrl')}
        </div>
        <div style={{ fontSize: 12, color: 'var(--color-text-muted)', marginBottom: 8 }}>
          {t('adminDash.topPageUrlHelp')}
        </div>
        <UrlShareControl path="/" downloadName="wsi-top" mode="inline" displayUrl="full" />
      </div>

      {/* The settings the Save button below persists, locked while it is
          in flight so nothing typed meanwhile is overwritten by its reply. */}
      <LockFieldset locked={netSaving} style={{ display: 'grid', gap: 18 }}>
        {/* IP Filter */}
        <div style={SECTION_STYLE}>
          <div style={SECTION_TITLE_STYLE}>{t('adminDash.ipFilter')}</div>

          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, cursor: 'pointer', marginBottom: 10 }}>
            <input
              type="checkbox"
              checked={netSettings.ip_filter_enabled}
              onChange={(e) => {
                const enabled = e.target.checked
                setNetSettings((s) => s && { ...s, ip_filter_enabled: enabled })
                // Drop any half-typed Add-row input when the filter is turned
                // off: the whole rule UI (including the Add row) unmounts with
                // the filter, so the value would otherwise linger invisibly in
                // state and be silently discarded at Save time.
                if (!enabled) { setNewRuleIp(''); setNewRuleNote(''); setNewRuleErr('') }
              }}
            />
            {t('adminDash.enableIpFilter')}
          </label>

          {netSettings.ip_filter_enabled && (
            <>
              <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12, marginBottom: 12 }}>
                <span style={{ color: 'var(--color-text-muted)', flexShrink: 0 }}>{t('adminDash.defaultUnmatched')}</span>
                <select
                  value={netSettings.ip_filter_default}
                  onChange={(e) => setNetSettings((s) => s && { ...s, ip_filter_default: e.target.value as 'allow' | 'deny' })}
                  style={{ fontSize: 12 }}
                >
                  <option value="allow">{t('adminDash.ipFilterAllow')}</option>
                  <option value="deny">{t('adminDash.ipFilterDeny')}</option>
                </select>
              </label>

              {netSettings.ip_rules.length > 0 && (
                <>
                  <div style={{ fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 4 }}>
                    {t('adminDash.ipRuleOrderHelp')}
                  </div>
                  <div style={{ border: '1px solid var(--color-border)', borderRadius: 6, overflow: 'hidden', marginBottom: 10 }}>
                    {netSettings.ip_rules.map((rule, i) => (
                      // Canonicalized rule.ip is unique among rules (the
                      // duplicate check at Add time enforces this), so it
                      // is a stable React key — survives reorder/delete
                      // without remounting unrelated rows.
                      <div key={rule.ip} style={{
                        display: 'flex', alignItems: 'center', gap: 8,
                        padding: '6px 10px',
                        background: i % 2 === 0 ? 'var(--color-surface)' : 'var(--color-surface-muted)',
                        borderBottom: i < netSettings.ip_rules.length - 1 ? '1px solid var(--color-border)' : 'none',
                        fontSize: 12,
                      }}>
                        <div style={{ display: 'flex', flexDirection: 'column', gap: 0, flexShrink: 0 }}>
                          <button
                            type="button"
                            aria-label={t('adminDash.ipRuleMoveUp')}
                            title={t('adminDash.ipRuleMoveUp')}
                            disabled={i === 0}
                            onClick={() => moveRule(i, -1)}
                            style={{
                              background: 'none', border: 'none', cursor: i === 0 ? 'default' : 'pointer',
                              color: i === 0 ? 'var(--color-text-muted)' : 'inherit',
                              fontSize: 10, lineHeight: 1, padding: '0 2px',
                            }}
                          >▲</button>
                          <button
                            type="button"
                            aria-label={t('adminDash.ipRuleMoveDown')}
                            title={t('adminDash.ipRuleMoveDown')}
                            disabled={i === netSettings.ip_rules.length - 1}
                            onClick={() => moveRule(i, 1)}
                            style={{
                              background: 'none', border: 'none', cursor: i === netSettings.ip_rules.length - 1 ? 'default' : 'pointer',
                              color: i === netSettings.ip_rules.length - 1 ? 'var(--color-text-muted)' : 'inherit',
                              fontSize: 10, lineHeight: 1, padding: '0 2px',
                            }}
                          >▼</button>
                        </div>
                        <span style={{
                          display: 'inline-block', padding: '1px 7px', borderRadius: 4, fontSize: 11,
                          background: rule.action === 'allow' ? 'var(--color-success-bg)' : 'var(--color-danger-bg)',
                          color: rule.action === 'allow' ? 'var(--color-success-fg)' : 'var(--color-danger-fg)',
                          fontWeight: 600, flexShrink: 0,
                        }}>
                          {rule.action === 'allow' ? t('adminDash.ruleAllow') : t('adminDash.ruleDeny')}
                        </span>
                        <code style={{ fontSize: 12, flex: '0 0 auto' }}>{rule.ip}</code>
                        {rule.note && <span style={{ color: 'var(--color-text-muted)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', minWidth: 0 }}>{rule.note}</span>}
                        <button
                          type="button"
                          onClick={() => setNetSettings((s) => s && { ...s, ip_rules: s.ip_rules.filter((_, j) => j !== i) })}
                          style={{ marginLeft: 'auto', flexShrink: 0, fontSize: 11, color: 'var(--color-danger-500)', background: 'none', border: 'none', cursor: 'pointer', padding: '0 4px' }}
                        >×</button>
                      </div>
                    ))}
                  </div>
                </>
              )}

              <div style={{ display: 'flex', gap: 6, flexWrap: 'wrap', alignItems: 'flex-end', marginBottom: 6 }}>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: '1 1 120px' }}>
                  <span style={FIELD_LABEL_STYLE}>{t('adminDash.fieldIpCidr')}</span>
                  <input
                    value={newRuleIp}
                    onChange={(e) => { setNewRuleIp(e.target.value); if (newRuleErr) setNewRuleErr('') }}
                    placeholder={t('adminDash.placeholderIpCidr')}
                    style={{ fontSize: 12, width: '100%', boxSizing: 'border-box' }}
                  />
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
                  <span style={FIELD_LABEL_STYLE}>{t('adminDash.fieldAction')}</span>
                  <select
                    value={newRuleAction}
                    onChange={(e) => setNewRuleAction(e.target.value as 'allow' | 'deny')}
                    style={{ fontSize: 12 }}
                  >
                    <option value="allow">{t('adminDash.ruleAllow')}</option>
                    <option value="deny">{t('adminDash.ruleDeny')}</option>
                  </select>
                </label>
                <label style={{ display: 'flex', flexDirection: 'column', gap: 2, flex: '1 1 100px' }}>
                  <span style={FIELD_LABEL_STYLE}>{t('adminDash.fieldNote')}</span>
                  <input
                    value={newRuleNote}
                    onChange={(e) => setNewRuleNote(e.target.value)}
                    placeholder={t('adminDash.placeholderNote')}
                    style={{ fontSize: 12, width: '100%', boxSizing: 'border-box' }}
                  />
                </label>
                <button
                  type="button"
                  onClick={() => {
                    const ip = newRuleIp.trim()
                    if (!ip) return
                    // Cheap front-end sanity check. The server still does
                    // proper parsing; this just catches obvious typos so the
                    // operator gets feedback before the round-trip Save.
                    // Pattern accepts dotted-IPv4 (a.b.c.d) and IPv6 hex
                    // groups, each optionally followed by /N; IPv4 octets must
                    // be 0-255 with a prefix <= 32, IPv6 prefixes <= 128.
                    const ipv4InRange = (() => {
                      try {
                        const [a, m] = ip.split('/', 2)
                        ipv4NetworkAddr(a, m === undefined ? 32 : Number(m))
                        return true
                      } catch {
                        return false
                      }
                    })()
                    const looksLikeIpv4 = /^(\d{1,3}\.){3}\d{1,3}(\/\d{1,2})?$/.test(ip) && ipv4InRange
                    const looksLikeIpv6 = /^[0-9a-fA-F:]+(\/\d{1,3})?$/.test(ip) && ip.includes(':')
                      && (!ip.includes('/') || Number(ip.split('/')[1]) <= 128)
                    if (!looksLikeIpv4 && !looksLikeIpv6) {
                      setNewRuleErr(t('adminDash.ipRuleInvalid'))
                      return
                    }
                    const newKey = canonicalIpRuleKey(ip)
                    if ((netSettings.ip_rules || []).some((r) => canonicalIpRuleKey(r.ip) === newKey)) {
                      setNewRuleErr(t('adminDash.ipRuleDuplicate'))
                      return
                    }
                    setNewRuleErr('')
                    const rule: IpRule = { ip, action: newRuleAction, note: newRuleNote.trim() }
                    setNetSettings((s) => s && { ...s, ip_rules: [...s.ip_rules, rule] })
                    setNewRuleIp('')
                    setNewRuleNote('')
                  }}
                  style={{ fontSize: 12, alignSelf: 'flex-end' }}
                >
                  {t('adminDash.addRule')}
                </button>
              </div>

              {newRuleErr && (
                <div role="alert" style={{ fontSize: 11, color: 'var(--color-danger-fg)', marginBottom: 6 }}>
                  {newRuleErr}
                </div>
              )}
              {myIp && (
                <div style={{ display: 'flex', flexDirection: 'column', gap: 4, alignItems: 'flex-start' }}>
                  <button
                    type="button"
                    onClick={() => { setNewRuleIp(myIp); setNewRuleErr('') }}
                    style={{ fontSize: 11, background: 'var(--color-brand-50)', border: '1px solid var(--color-brand-200)', borderRadius: 5, padding: '3px 10px', color: 'var(--color-brand-700)', cursor: 'pointer' }}
                  >
                    {t('adminDash.fillCurrentIp', { ip: myIp })}
                  </button>
                  {myIpViaCfTunnel && (
                    <div style={{ fontSize: 11, color: 'var(--color-warning-fg, var(--color-danger-fg))', maxWidth: 480 }}>
                      {t('adminDash.fillCurrentIpCfWarning')}
                    </div>
                  )}
                </div>
              )}

              <div style={{ marginTop: 10, fontSize: 11, color: 'var(--color-text-muted)', background: 'var(--color-surface-muted)', borderRadius: 5, padding: '6px 10px' }}>
                {t('adminDash.localhostNote')}
              </div>
            </>
          )}
        </div>

        {/* mDNS */}
        <div style={SECTION_STYLE}>
          <div style={SECTION_TITLE_STYLE}>{t('adminDash.mdns')}</div>

          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, cursor: 'pointer', marginBottom: 6 }}>
            <input
              type="checkbox"
              checked={netSettings.mdns_enabled}
              onChange={(e) => setNetSettings((s) => s && { ...s, mdns_enabled: e.target.checked })}
            />
            {t('adminDash.mdnsEnable')}
          </label>
          <div style={{ fontSize: 11, color: 'var(--color-text-muted)', marginBottom: 8 }}>
            {t('adminDash.mdnsHelp')}
          </div>

          <div style={{ fontSize: 12, display: 'flex', alignItems: 'center', gap: 6 }}>
            <span style={{
              display: 'inline-block', width: 8, height: 8, borderRadius: '50%',
              background: netSettings.mdns_active ? 'var(--color-brand-500)' : 'var(--color-border-strong)',
            }} />
            <span style={{ color: netSettings.mdns_active ? 'var(--color-success-fg)' : 'var(--color-text-muted)' }}>
              {netSettings.mdns_active ? t('adminDash.mdnsAnnouncing') : t('adminDash.mdnsStopped')}
            </span>
          </div>
        </div>

        {/* Google Analytics */}
        <div style={SECTION_STYLE}>
          <div style={SECTION_TITLE_STYLE}>{t('adminDash.googleAnalytics')}</div>
          <div style={{ fontSize: 12, color: 'var(--color-text-muted)', marginBottom: 8 }}>{t('adminDash.gaHelp')}</div>
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
            <span style={{ color: 'var(--color-text-muted)', flexShrink: 0 }}>{t('adminDash.gaMeasurementId')}</span>
            <input
              type="text"
              value={netSettings.ga_measurement_id}
              // GA4 measurement IDs are uppercase by spec and the server regex
              // is case-sensitive (^G-[A-Z0-9]{4,32}$). Normalize on input so
              // a pasted lowercase value doesn't round-trip a 422 from the
              // server when the operator presses Save.
              onChange={(e) => setNetSettings((s) => s && { ...s, ga_measurement_id: e.target.value.toUpperCase() })}
              placeholder={t('adminDash.gaPlaceholder')}
              maxLength={64}
              style={{ fontFamily: 'monospace', fontSize: 13, width: 180 }}
            />
          </label>
          {netSettings.ga_measurement_id && !/^G-[A-Z0-9]{4,32}$/.test(netSettings.ga_measurement_id.trim()) && (
            <div style={{ marginTop: 6, fontSize: 11, color: 'var(--color-danger-fg)' }}>
              {t('adminDash.gaInvalid')}
            </div>
          )}
        </div>

        {/* Access analytics: recording mode + notice toast */}
        <div style={SECTION_STYLE}>
          <div style={SECTION_TITLE_STYLE}>
            {t('adminDash.analyticsMode')}
          </div>
          <div style={{ fontSize: 12, color: 'var(--color-text-muted)', marginBottom: 8 }}>
            {t('adminDash.analyticsModeHelp')}
          </div>
          {(['off', 'hashed', 'plain'] as const).map((mode) => {
            const isHashedAndUnavailable = mode === 'hashed' && !netSettings.analytics_hash_salt_available
            const label =
              mode === 'off'
                ? t('adminDash.analyticsModeOff')
                : mode === 'hashed'
                  ? t('adminDash.analyticsModeHashed')
                  : t('adminDash.analyticsModePlain')
            const help =
              mode === 'off'
                ? t('adminDash.analyticsModeOffHelp')
                : mode === 'hashed'
                  ? t('adminDash.analyticsModeHashedHelp')
                  : t('adminDash.analyticsModePlainHelp')
            return (
              <label
                key={mode}
                style={{
                  display: 'flex',
                  alignItems: 'flex-start',
                  gap: 8,
                  fontSize: 13,
                  marginBottom: 6,
                  cursor: isHashedAndUnavailable ? 'not-allowed' : 'pointer',
                  opacity: isHashedAndUnavailable ? 0.55 : 1,
                }}
                title={isHashedAndUnavailable ? t('adminDash.analyticsSaltMissing') : undefined}
              >
                <input
                  type="radio"
                  name="analytics_mode"
                  value={mode}
                  checked={netSettings.analytics_mode === mode}
                  disabled={isHashedAndUnavailable}
                  onChange={() => setNetSettings((s) => s && { ...s, analytics_mode: mode })}
                  style={{ marginTop: 2 }}
                />
                <span>
                  <span style={{ fontWeight: 600 }}>{label}</span>
                  <span style={{ display: 'block', fontSize: 11, color: 'var(--color-text-muted)', marginTop: 2 }}>
                    {help}
                  </span>
                </span>
              </label>
            )
          })}
          {!netSettings.analytics_hash_salt_available && (
            <div style={{ marginTop: 4, fontSize: 11, color: 'var(--color-danger-fg)' }}>
              {t('adminDash.analyticsSaltMissing')}
            </div>
          )}

          <label
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 8,
              fontSize: 13,
              marginTop: 12,
              cursor: netSettings.analytics_mode === 'off' ? 'not-allowed' : 'pointer',
              opacity: netSettings.analytics_mode === 'off' ? 0.55 : 1,
            }}
          >
            <input
              type="checkbox"
              checked={netSettings.analytics_notice_toast_enabled}
              disabled={netSettings.analytics_mode === 'off'}
              onChange={(e) => setNetSettings((s) => s && { ...s, analytics_notice_toast_enabled: e.target.checked })}
            />
            {t('adminDash.analyticsNoticeToast')}
          </label>
          <div style={{ fontSize: 11, color: 'var(--color-text-muted)', marginLeft: 24, marginTop: 2 }}>
            {t('adminDash.analyticsNoticeToastHelp')}
          </div>
        </div>
      </LockFieldset>

      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
      <button
        disabled={netSaving || !netIsDirty}
        style={netIsDirty ? DIRTY_SAVE_STYLE : undefined}
        onClick={async () => {
          if (netSaving) return
          // Block save when there's an unsaved IP / CIDR typed in
          // the Add row, instead of silently discarding it. The operator
          // either commits it via Add or clears the field first.
          if (netSettings.ip_filter_enabled && newRuleIp.trim()) {
            setNetMsg(t('adminDash.pendingRuleNotice'))
            setNetMsgIsError(true)
            return
          }
          // Lockout guard: warn before a save that would 403 the operator's
          // own IP on its very next request. rulesAllowIp mirrors the
          // server's network.rules_allow_ip (loopback always allowed, first
          // matching rule decides — IPv4 / IPv6 addresses and CIDRs — then
          // the default policy; an IPv4-mapped loopback, which Ubuntu 24.04's
          // Python 3.12.3 does not exempt, is judged by the rules), so this
          // predicts (or errs towards warning about) the server's own
          // self-lockout guard, which evaluates the same rules against the
          // same IP that my-ip reported. Running it for every default (not
          // just deny) also catches the default=allow case where an explicit
          // deny rule names the operator's own IP.
          // Once the operator confirms, the save carries override_ip_lockout
          // so the server's matching guard doesn't reject it with a 422.
          let overrideIpLockout = false
          if (
            netSettings.ip_filter_enabled
            && !rulesAllowIp(myIp, true, netSettings.ip_filter_default, netSettings.ip_rules)
          ) {
            const trimmedMyIp = myIp.trim()
            const matchedExplicitDeny = firstMatchingIpRule(myIp, netSettings.ip_rules)?.action === 'deny'
            const msg = matchedExplicitDeny
              ? t('adminDash.ipFilterDenySelfConfirm', { ip: trimmedMyIp })
              : trimmedMyIp
                ? t('adminDash.ipFilterDenyConfirm', { ip: trimmedMyIp })
                : t('adminDash.ipFilterDenyConfirmNoMyIp')
            if (!window.confirm(msg)) return
            overrideIpLockout = true
          }
          setNetMsg('')
          setNetMsgIsError(false)
          await saveRequest.run(async () => {
            try {
              // PATCH returns the full settings, so no follow-up
              // GET is needed. mdns_ok / mdns_msg come back alongside.
              const sentRuleCount = netSettings.ip_rules.length
              const send = (override: boolean) => adminUpdateNetworkSettings({
                ip_filter_enabled: netSettings.ip_filter_enabled,
                ip_filter_default: netSettings.ip_filter_default,
                ip_rules: netSettings.ip_rules,
                mdns_enabled: netSettings.mdns_enabled,
                ga_measurement_id: netSettings.ga_measurement_id,
                analytics_mode: netSettings.analytics_mode,
                analytics_notice_toast_enabled: netSettings.analytics_notice_toast_enabled,
                override_ip_lockout: override,
              })
              let res: Awaited<ReturnType<typeof send>>
              try {
                res = await send(overrideIpLockout)
              } catch (e) {
                // The prediction above missed and the server's guard refused
                // the save (e.g. the IP it sees differs from what my-ip reported
                // when the page loaded). Ask with the IP the server named and
                // resend with the override; declining reports the refusal.
                if (
                  overrideIpLockout || !isApiCodeError(e) || e.code !== IP_LOCKOUT_ERROR_CODE
                  || !window.confirm(t('adminDash.ipFilterLockoutConfirm', { ip: String(e.params?.ip ?? '') }))
                ) throw e
                res = await send(true)
              }
              const saved: NetworkSettings = {
                ip_filter_enabled: res.ip_filter_enabled,
                ip_filter_default: res.ip_filter_default,
                ip_rules: res.ip_rules,
                mdns_enabled: res.mdns_enabled,
                mdns_active: res.mdns_active,
                ga_measurement_id: res.ga_measurement_id,
                analytics_mode: res.analytics_mode,
                analytics_notice_toast_enabled: res.analytics_notice_toast_enabled,
                analytics_hash_salt_available: res.analytics_hash_salt_available,
              }
              setNetSettings(saved)
              setNetSavedSnapshot(networkSnapshot(saved))
              // Server-side canonical dedupe in admin_settings.py can drop
              // host-bit-equivalent IPv6 CIDRs that the client-side string
              // dedupe (intentionally IPv4-only) missed. Surface the
              // discrepancy so a rule disappearing from the list isn't
              // mysterious.
              const dropped = sentRuleCount - res.ip_rules.length
              if (!res.mdns_ok && res.mdns_msg) {
                setNetMsg(t('adminDash.networkSavedMdns', { msg: res.mdns_msg }))
                setNetMsgIsError(true)
              } else if (dropped > 0) {
                setNetMsg(t('adminDash.ipRulesDeduped', { n: dropped }))
                setNetMsgIsError(false)
              } else {
                setNetMsg(t('adminDash.networkSaved'))
                setNetMsgIsError(false)
              }
            } catch (e: any) {
              const detail = apiErrorMessage(e, t)
              setNetMsg(detail ? t('adminDash.networkError', { msg: detail }) : t('adminDash.saveFailedGeneric'))
              setNetMsgIsError(true)
            }
          })
        }}
      >
        {netSaving ? t('adminDash.saving') : t('adminDash.saveNetworkSettings')}
      </button>
      {netIsDirty && !netSaving && <UnsavedChangesChip />}
      </div>
      <SaveResult text={netMsg} isError={netMsgIsError} />
    </>
  )
}
