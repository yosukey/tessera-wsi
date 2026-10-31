import React, { useEffect, useState, type MutableRefObject } from 'react'
import { useI18n } from '../../lib/i18n'
import { useUnsavedNavigationGuard } from '../../hooks/useUnsavedNavigationGuard'
import AdminNetworkPanel from './AdminNetworkPanel'
import AdminAuthPanel from './AdminAuthPanel'

type Props = {
  // Reports whether there are unsaved edits, so the dashboard can confirm
  // before a sidebar tab switch unmounts this section and drops them.
  onDirtyChange?: (dirty: boolean) => void
  // Holds the navigation guard's in-app exit while this section is mounted
  // (null otherwise). The dashboard leaves the tab through it — header
  // links, Logout, a sidebar tab switch — so the history entry the guard
  // pushed for unsaved edits is stepped off first instead of left behind
  // for Back to land on. It does not ask: the dashboard confirms first.
  leaveRef?: MutableRefObject<((go: () => void) => void) | null>
}

// The dashboard's Network tab: the network settings and the app-access
// authentication settings, two forms with a Save button each. Their unsaved
// edits are guarded here together.
export default function AdminNetworkAuthSection({ onDirtyChange, leaveRef }: Props = {}) {
  const { t } = useI18n()
  const [netIsDirty, setNetIsDirty] = useState(false)
  const [authIsDirty, setAuthIsDirty] = useState(false)
  const guardDirty = netIsDirty || authIsDirty

  // Surface the dirty flag to the dashboard so it can intercept a sidebar tab
  // switch, a header link or Logout (in-SPA unmounts beforeunload can't catch).
  useEffect(() => {
    onDirtyChange?.(guardDirty)
  }, [guardDirty, onDirtyChange])
  // Clear the parent's flag on unmount so a remount doesn't inherit a stale
  // "dirty" from a previous mount.
  useEffect(() => () => onDirtyChange?.(false), [onDirtyChange])
  // beforeunload covers full-page exits (browser close / reload) while dirty;
  // the navigation guard below covers browser Back / Forward.
  useEffect(() => {
    if (!guardDirty) return
    const onBeforeUnload = (e: BeforeUnloadEvent) => { e.preventDefault(); e.returnValue = '' }
    window.addEventListener('beforeunload', onBeforeUnload)
    return () => window.removeEventListener('beforeunload', onBeforeUnload)
  }, [guardDirty])
  const leave = useUnsavedNavigationGuard(guardDirty, t('adminDash.unsavedChangesConfirm'))
  useEffect(() => {
    if (!leaveRef) return
    leaveRef.current = leave
    return () => {
      if (leaveRef.current === leave) leaveRef.current = null
    }
  }, [leaveRef, leave])

  return (
    <div style={{ display: 'grid', gap: 18, marginTop: 16 }}>
      <AdminNetworkPanel onDirtyChange={setNetIsDirty} />
      <AdminAuthPanel onDirtyChange={setAuthIsDirty} />
    </div>
  )
}
