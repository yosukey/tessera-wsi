import { useEffect, useRef, useState, type CSSProperties } from 'react'

// Off-screen but still in the accessibility tree (display:none / hidden would
// take a live region out of it, and nothing would be announced).
const VISUALLY_HIDDEN: CSSProperties = {
  position: 'absolute',
  width: 1,
  height: 1,
  margin: -1,
  padding: 0,
  overflow: 'hidden',
  clip: 'rect(0 0 0 0)',
  whiteSpace: 'nowrap',
  border: 0,
}

interface Props {
  // The visible progress line. May change on every poll (bytes received,
  // slides done), about once a second.
  text: string
  // What is announced on: a new value is a new phase ("receiving" ->
  // "unpacking" -> "importing"). Same-phase updates are not announced.
  phase: string
  style?: CSSProperties
}

// A progress line that screen readers announce once per phase, not once per
// poll. A role="status" element whose text is a running byte count or
// "3 of 10 done" would be read out every second for the whole multi-minute job,
// drowning out everything else; the figures stay readable on demand in the
// visible line, which is explicitly not live (aria-live="off" also keeps an
// enclosing live region — a role="status" notice it sits in — from
// announcing its every change). A separate polite live region, off-screen,
// receives the line's text only when `phase` changes. It is filled after
// mounting, not with its initial content, because a live region inserted
// already holding text is not announced by every screen reader.
//
// Passing the message itself as `phase` makes a line announced once per
// distinct message (the upload screen's free-space warning): a re-render
// with the same text changes nothing in the live region.
export default function PhaseStatus({ text, phase, style }: Props) {
  const textRef = useRef(text)
  textRef.current = text
  const [announced, setAnnounced] = useState('')
  useEffect(() => {
    setAnnounced(textRef.current)
  }, [phase])
  return (
    <>
      <span aria-live="off" style={style}>{text}</span>
      <span role="status" style={VISUALLY_HIDDEN}>{announced}</span>
    </>
  )
}
