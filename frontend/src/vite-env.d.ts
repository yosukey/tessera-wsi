/// <reference types="vite/client" />

interface ImportMetaEnv {
  // Optional LAN-direct hostname for the QuickConnect-style redirect probe
  // (see src/lib/lanProbe.ts). Unset in the default build -> probe is a no-op.
  readonly VITE_LAN_HOST?: string
  // Optional override (in ms) for the first-ever probe's long window, which
  // must outlast the browser's local-network permission prompt. Unset ->
  // default in src/lib/lanProbe.ts. Operators set WSI_LAN_PROBE_TIMEOUT_MS.
  readonly VITE_LAN_PROBE_TIMEOUT_MS?: string
  // Optional canonical public base URL of this deployment (e.g.
  // https://wsi.example.com), baked in from WSI_PUBLIC_BASE_URL at build time.
  // When set and the operator is on a direct/local route (LAN host, bare IP,
  // .local), the share controls (src/components/admin/UrlShareControl.tsx) also
  // offer this public URL for copy/QR so a link that works off-LAN can be
  // handed out. Unset in the default build -> only the current-origin URL is
  // shown, unchanged.
  readonly VITE_PUBLIC_BASE_URL?: string
}

declare module '*.module.css' {
  const classes: { readonly [key: string]: string }
  export default classes
}
