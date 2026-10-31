import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import { execFileSync } from 'node:child_process'
import { existsSync, readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

// For native-Ubuntu dev, the FastAPI backend runs on 127.0.0.1:8000.
// Override with VITE_API_PROXY_TARGET if the backend lives elsewhere.
const apiTarget = process.env.VITE_API_PROXY_TARGET || 'http://127.0.0.1:8000'

// Resolve the build-time app version. Same order as
// services/app/wsi_app/version.py (resolve_version), so the About dialog and
// the admin server tab agree on what's deployed:
//   1. <repo>/VERSION  — written by infra/install.sh from
//      `git describe --tags --always` at deploy time. This is the
//      authoritative answer in production because the deploy strips .git
//      from $APP_HOME but leaves VERSION intact.
//   2. `git describe --tags` against the source repo — for dev builds where
//      install.sh hasn't run but a .git directory with a reachable tag exists.
//   3. frontend/package.json — last-resort fallback (e.g. tarball builds
//      with neither VERSION nor .git). Releases are git tags and do not bump
//      it; the backend treats a version from here as untrusted for its
//      downgrade guards.
function resolveAppVersion(): string {
  const repoRoot = fileURLToPath(new URL('..', import.meta.url))

  const versionFile = fileURLToPath(new URL('../VERSION', import.meta.url))
  try {
    const stamped = readFileSync(versionFile, 'utf-8').trim()
    if (stamped) return stamped.replace(/^v/, '')
  } catch {
    // Fall through to git describe / package.json.
  }

  if (existsSync(`${repoRoot}/.git`)) {
    try {
      const described = execFileSync(
        'git',
        ['-C', repoRoot, 'describe', '--tags'],
        { encoding: 'utf-8', stdio: ['ignore', 'pipe', 'ignore'] },
      ).trim()
      if (described) return described.replace(/^v/, '')
    } catch {
      // Fall through to package.json.
    }
  }

  const pkg = JSON.parse(
    readFileSync(fileURLToPath(new URL('./package.json', import.meta.url)), 'utf-8'),
  ) as { version: string }
  return pkg.version
}

const appVersion = resolveAppVersion()

export default defineConfig({
  plugins: [react()],
  define: {
    __APP_VERSION__: JSON.stringify(appVersion),
  },
  build: {
    // Avoid colliding with the backend's /assets mount (tour attachments live
    // there and are served by FastAPI from data_dir/assets/). Vite's
    // default assetsDir is 'assets', which would make the built SPA bundle
    // resolve to /assets/index-*.js and be routed to the tour assets mount,
    // returning 404.
    assetsDir: 'static-assets',
  },
  server: {
    host: '127.0.0.1',
    port: 5173,
    proxy: {
      // ws: true is required so WebSocket upgrades on /api/sync/ws/* and
      // /api/sync/discovery pass through to FastAPI. Without it the
      // upgrade handshake silently 502s and follower / discovery sockets
      // never connect, so the banner on the index page can't appear.
      '/api': { target: apiTarget, changeOrigin: true, ws: true },
      '/dzi': apiTarget,
      '/thumbs': apiTarget,
      '/assets': apiTarget,
    },
  },
})
