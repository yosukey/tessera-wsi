import 'react'

// React 18's JSX attribute types (@types/react ^18.3) don't yet include the
// standard `inert` boolean attribute; React 19 adds it natively. The app
// relies on `inert` to take whole subtrees out of pointer, keyboard, and a11y
// interaction (e.g. locking the admin dashboard while an upload is in flight),
// so declare it here. Modeled as `''` (the canonical "present" boolean-attribute
// value, which avoids react-dom's non-boolean-attribute dev warning) so it can
// be set to '' to lock and undefined to unlock.
declare module 'react' {
  interface HTMLAttributes<T> {
    inert?: '' | undefined
  }
}
