// Global-fetch stand-in for API-client tests: answers each request with the
// next canned JSON response and records what was sent — the URL, the method,
// and the body decoded as JSON or as form fields — so a test can assert on the
// wire format. Pair with `vi.unstubAllGlobals()` in afterEach.
import { vi } from 'vitest'

export type FakeRequest = {
  url: string
  method: string
  json: any
  form: Record<string, string> | null
}

export type FakeResponse = { status?: number; body: unknown }

export function fakeFetch(responses: FakeResponse[]) {
  const requests: FakeRequest[] = []
  const queue = [...responses]
  const fn = vi.fn(async (input: RequestInfo | URL, init: RequestInit = {}) => {
    const body = init.body
    requests.push({
      url: String(input),
      method: (init.method || 'GET').toUpperCase(),
      json: typeof body === 'string' ? JSON.parse(body) : null,
      form: body instanceof FormData
        ? Object.fromEntries([...body.entries()].map(([k, v]) => [k, String(v)]))
        : null,
    })
    const next = queue.shift()
    if (!next) throw new Error(`unexpected fetch: ${init.method || 'GET'} ${String(input)}`)
    return new Response(JSON.stringify(next.body), {
      status: next.status ?? 200,
      headers: { 'content-type': 'application/json' },
    })
  })
  vi.stubGlobal('fetch', fn)
  return { requests, fn }
}
