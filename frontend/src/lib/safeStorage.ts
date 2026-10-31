// localStorage access throws SecurityError when storage is blocked (Chrome
// "Block all cookies", some private-browsing modes). Reading it unguarded in a
// render-phase initializer crashes the page (into the ErrorBoundary's
// fallback, or a blank screen for code above it such as I18nProvider). These
// helpers degrade to a no-op / null instead.

export function safeGetItem(key: string): string | null {
  try {
    return localStorage.getItem(key)
  } catch {
    return null
  }
}

export function safeSetItem(key: string, value: string): void {
  try {
    localStorage.setItem(key, value)
  } catch {
    /* storage unavailable or quota exceeded: preference just won't persist */
  }
}

export function safeRemoveItem(key: string): void {
  try {
    localStorage.removeItem(key)
  } catch {
    /* storage unavailable: nothing to clear */
  }
}

// sessionStorage variants — same guarding, but scoped to the tab session
// (used e.g. for a running backup job id, which is only meaningful for the
// life of the current admin session).
export function safeSessionGetItem(key: string): string | null {
  try {
    return sessionStorage.getItem(key)
  } catch {
    return null
  }
}

export function safeSessionSetItem(key: string, value: string): void {
  try {
    sessionStorage.setItem(key, value)
  } catch {
    /* storage unavailable or quota exceeded: value just won't persist */
  }
}

export function safeSessionRemoveItem(key: string): void {
  try {
    sessionStorage.removeItem(key)
  } catch {
    /* storage unavailable: nothing to clear */
  }
}
