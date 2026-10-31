// Single source of truth for browser storage keys used across the app.
// Renaming a key here is a one-line change; touching every call site is
// how stale keys get left behind.

export const STORAGE_KEYS = {
  adminToken: 'admin_token',
  locale: 'wsi-locale',
  viewMode: 'wsi-view-mode',
  thumbnailSize: 'wsi-thumbnail-size',
  // Time of the ErrorBoundary's last automatic reload after a missing lazy
  // chunk (sessionStorage), which caps those reloads to one per cooldown.
  chunkReload: 'wsi.chunkReload',
  // Job id of an in-progress admin backup, persisted (sessionStorage) so a
  // sidebar tab switch that unmounts AdminBackupSection can re-attach to the
  // running job on remount instead of losing its progress + double-start guard.
  backupJobId: 'wsi.admin.backupJobId',
  // Job id of a restore staging job (sessionStorage), kept while it runs and
  // while the stage it produced awaits apply or cancel, so a tab switch or
  // reload resumes the job / shows the staged preview again instead of
  // leaving the stage unreachable until the 24 h sweep.
  restoreStageJobId: 'wsi.admin.restoreStageJobId',
  // Set (sessionStorage) right before the dashboard reloads after a restore /
  // reset apply: the JSON outcome (or "null"), or { awaitingStageId } when the
  // session expired mid-wait and the restore's result is still to be matched.
  // Its presence reopens the Backup & Restore tab after the reload, which shows the
  // banner and removes the key.
  adminPostRestore: 'wsi.admin.postRestore',
  // Stage id of a slide-import bundle on the server — staged and not yet
  // applied, or kept for a retry (sessionStorage), so the Import tab can
  // return to it, or discard it, after a tab switch
  // or reload (e.g. around the restart that applies a raised attachment
  // limit) instead of leaving it unreachable until the 24 h sweep.
  importKeptStageId: 'wsi.admin.importKeptStageId',
  // The kept stage that importKeptStageId pointed at before a job on another
  // stage started, so it is offered again if that job's stage turns out to
  // be gone (see AdminImportSection.runStageJob).
  importKeptStageFallbackId: 'wsi.admin.importKeptStageFallbackId',
  // Job id of a slide-import upload being staged on the server
  // (sessionStorage), so a tab switch or reload while the bundle is unpacked
  // re-attaches to it; the stage it produces then goes to importKeptStageId.
  importStageJobId: 'wsi.admin.importStageJobId',
  // The dashboard slide list's search, sort and filters (sessionStorage), so
  // leaving for another admin page or a viewer and coming back keeps them
  // (see lib/admin/slideListState). Removed on logout.
  adminSlideListState: 'wsi.admin.slideListState',
  syncDisplayName: 'wsi.sync.displayName',
  syncPresenterSidebarCollapsed: 'wsi.sync.presenterSidebarCollapsed',
  // Indicator (localStorage, so it persists across sessions on this browser)
  // that the analytics recording notice toast has already been dismissed.
  // Stored as a JSON envelope {"mode":"hashed","at":1700000000000} so a mode
  // change (off→hashed, hashed→plain) re-fires the toast.
  analyticsNoticeDismissed: 'wsi.analytics.noticeDismissed',
} as const
