import React from 'react'
import ReactDOM from 'react-dom/client'
import { BrowserRouter } from 'react-router'
import App from './pages/App'
import { I18nProvider } from './lib/i18n'
import ErrorBoundary, { handlePreloadError } from './components/ErrorBoundary'
import { runLanProbe } from './lib/lanProbe'
import './styles/global.css'
import './styles/imageAdjust.css'

// Optional LAN-direct redirect (no-op unless VITE_LAN_HOST is set at build
// time). Run before mounting React so a same-VLAN client is handed off to the
// LAN host before doing any further work on the public origin.
runLanProbe()

// Vite fires this when a preloaded chunk 404s (stale tab after a redeploy).
window.addEventListener('vite:preloadError', handlePreloadError)

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <I18nProvider>
      <ErrorBoundary>
        {/* Commit location changes synchronously, as react-router v6 did. With
            the v7 default every update is a startTransition, so a heavy page
            (an index with thousands of cards) renders a stale location for
            a while after the URL has moved on. */}
        <BrowserRouter useTransitions={false}>
          <App />
        </BrowserRouter>
      </ErrorBoundary>
    </I18nProvider>
  </React.StrictMode>,
)
