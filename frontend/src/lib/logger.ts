// Tiny logger so production bundles don't ship verbose console.error noise
// for expected error paths (network blips, AbortController cancellations,
// etc.) while still surfacing them during development.
//
// Replace `console.error(...)` call sites with `logger.error(...)`. In dev
// mode (`import.meta.env.DEV`) it forwards to the real console; in
// production it is a no-op.

type LogArgs = unknown[]

const isDev = typeof import.meta !== 'undefined' && (import.meta as any).env?.DEV === true

function noop(..._args: LogArgs) {
  /* intentionally empty */
}

export const logger = {
  debug: isDev ? console.debug.bind(console) : noop,
  info: isDev ? console.info.bind(console) : noop,
  warn: isDev ? console.warn.bind(console) : noop,
  // Errors are logged in both modes so unexpected failures aren't lost
  // in production, but the shape stays consistent with debug/info/warn.
  error: console.error.bind(console),
}
