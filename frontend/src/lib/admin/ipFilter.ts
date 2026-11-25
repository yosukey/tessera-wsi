// Client-side mirror of the server's IP-filter decision, used by the Network
// tab to predict whether a save would lock the operator out before sending it
// (the server's self-lockout guard refuses such a save with a 422 unless it
// carries override_ip_lockout).
//
// It reproduces, step for step:
// - schemas.IpRuleIn.ip — the rule is stripped and parsed with
//   ipaddress.ip_network(strict=False) / ip_address; a single-host network
//   (/32, /128) is stored as the bare address, so it then matches by address
//   equality rather than by network membership;
// - network._rule_target — an IPv4-mapped rule is rewritten to its IPv4 form:
//   a bare ::ffff:a.b.c.d becomes a.b.c.d, and a network inside ::ffff:0:0/96
//   (prefix 96 or longer) becomes the IPv4 network with the prefix less 96
//   (::ffff:10.0.0.0/104 -> 10.0.0.0/8, ::ffff:0:0/96 -> 0.0.0.0/0); any other
//   IPv6 rule stays IPv6;
// - network.canonical_ip — the client address is unwrapped the same way, so
//   an IPv4 client seen through a dual-stack socket as ::ffff:a.b.c.d is
//   judged (loopback exemption included) as a.b.c.d;
// - network.rules_allow_ip — loopback is always allowed; a disabled filter
//   allows; an unparseable client IP gets the default policy; every rule is
//   read (the server only refuses to SAVE more than MAX_IP_RULES); they are
//   tried in order and the first match decides (CIDR membership, or address
//   equality for a bare address); a rule that does not parse is skipped;
//   nothing matching falls through to the default policy;
// - Python's ipaddress parsing and comparison (3.12 – 3.14): IPv4 octets are
//   strictly decimal without leading zeros; an IPv4 prefix may also be given
//   as a netmask or hostmask, an IPv6 prefix only as a number; IPv6 accepts
//   one "::", an embedded dotted-quad tail and a "%zone" suffix; after the
//   unwrapping above IPv4 and IPv6 never match each other (::/0 does not
//   cover an unwrapped ::ffff:a.b.c.d client); a zone takes part in address
//   equality but not in network membership, and is dropped by the unwrapping.
//
// Because the unwrapping happens before the loopback check, an IPv4-mapped
// loopback (::ffff:127.x.y.z) is exempt on every supported Python, including
// the 3.12.3 that Ubuntu 24.04 ships (whose IPv6Address.is_loopback would
// say False for the wrapped form).

export type IpVersion = 4 | 6

export interface ParsedIp {
  version: IpVersion
  value: bigint
  // IPv6 zone ("%eth0"); always null for IPv4.
  scope: string | null
}

interface ParsedNetwork {
  version: IpVersion
  // The network address (host bits masked off).
  value: bigint
  prefix: number
  // The written address's zone, which survives only when no host bits had to
  // be masked off (in particular for a single-host /128).
  scope: string | null
}

type CompiledRule =
  | { kind: 'host'; ip: ParsedIp }
  | { kind: 'net'; version: IpVersion; value: bigint; prefix: number }

// network.MAX_IP_RULES: the server refuses to save a longer list (a stored one
// that is longer — from an older release or a restore — is still enforced in
// full).
export const MAX_IP_RULES = 1000

export interface IpFilterRule {
  ip: string
  action: string
}

const DECIMAL = /^[0-9]+$/
const HEX = /^[0-9a-fA-F]+$/
// The characters Python's str.strip() removes (str.isspace), which differ
// from String.prototype.trim (no U+FEFF; adds U+001C..U+001F and U+0085).
const PY_SPACE = new Set([
  0x09, 0x0a, 0x0b, 0x0c, 0x0d, 0x1c, 0x1d, 0x1e, 0x1f, 0x20, 0x85, 0xa0, 0x1680,
  0x2000, 0x2001, 0x2002, 0x2003, 0x2004, 0x2005, 0x2006, 0x2007, 0x2008, 0x2009, 0x200a,
  0x2028, 0x2029, 0x202f, 0x205f, 0x3000,
])

function pyStrip(s: string): string {
  let a = 0
  let b = s.length
  while (a < b && PY_SPACE.has(s.charCodeAt(a))) a++
  while (b > a && PY_SPACE.has(s.charCodeAt(b - 1))) b--
  return s.slice(a, b)
}

const maxPrefix = (v: IpVersion) => (v === 4 ? 32 : 128)

function maskOf(version: IpVersion, prefix: number): bigint {
  const bits = BigInt(maxPrefix(version))
  const p = BigInt(prefix)
  return ((1n << p) - 1n) << (bits - p)
}

// IPv4Address._ip_int_from_string.
function ipv4Int(s: string): bigint | null {
  if (!s) return null
  const octets = s.split('.')
  if (octets.length !== 4) return null
  let v = 0n
  for (const o of octets) {
    if (!o || !DECIMAL.test(o) || o.length > 3) return null
    if (o !== '0' && o[0] === '0') return null
    const n = Number(o)
    if (n > 255) return null
    v = (v << 8n) | BigInt(n)
  }
  return v
}

// IPv6Address._ip_int_from_string.
function ipv6Int(s: string): bigint | null {
  if (!s || s.length > 45) return null
  const parts = s.split(':')
  if (parts.length < 3) return null
  if (parts[parts.length - 1].includes('.')) {
    const v4 = ipv4Int(parts.pop()!)
    if (v4 === null) return null
    parts.push(((v4 >> 16n) & 0xffffn).toString(16), (v4 & 0xffffn).toString(16))
  }
  if (parts.length > 9) return null

  let skipIndex: number | null = null
  for (let i = 1; i < parts.length - 1; i++) {
    if (!parts[i]) {
      if (skipIndex !== null) return null
      skipIndex = i
    }
  }
  let hi: number
  let lo: number
  let skipped: number
  if (skipIndex !== null) {
    hi = skipIndex
    lo = parts.length - skipIndex - 1
    if (!parts[0]) {
      hi -= 1
      if (hi) return null
    }
    if (!parts[parts.length - 1]) {
      lo -= 1
      if (lo) return null
    }
    skipped = 8 - (hi + lo)
    if (skipped < 1) return null
  } else {
    if (parts.length !== 8 || !parts[0] || !parts[parts.length - 1]) return null
    hi = parts.length
    lo = 0
    skipped = 0
  }

  const hextet = (h: string): bigint | null =>
    HEX.test(h) && h.length <= 4 ? BigInt(parseInt(h, 16)) : null
  let v = 0n
  for (let i = 0; i < hi; i++) {
    const h = hextet(parts[i])
    if (h === null) return null
    v = (v << 16n) | h
  }
  v <<= 16n * BigInt(skipped)
  for (let i = parts.length - lo; i < parts.length; i++) {
    const h = hextet(parts[i])
    if (h === null) return null
    v = (v << 16n) | h
  }
  return v
}

// IPv6Address(str): optional "%zone" (non-empty, no second "%").
function parseIpv6(s: string): ParsedIp | null {
  if (s.includes('/')) return null
  const at = s.indexOf('%')
  let addr = s
  let scope: string | null = null
  if (at >= 0) {
    addr = s.slice(0, at)
    scope = s.slice(at + 1)
    if (!scope || scope.includes('%')) return null
  }
  const value = ipv6Int(addr)
  return value === null ? null : { version: 6, value, scope }
}

// ipaddress.ip_address(str): IPv4 first, then IPv6.
export function parseIpAddress(s: string): ParsedIp | null {
  if (s.includes('/')) return null
  const v4 = ipv4Int(s)
  if (v4 !== null) return { version: 4, value: v4, scope: null }
  return parseIpv6(s)
}

// _BaseNetwork._prefix_from_ip_int.
function prefixFromIpInt(n: bigint, bits: number): number | null {
  let tz = 0
  if (n === 0n) {
    tz = bits
  } else {
    while (tz < bits && ((n >> BigInt(tz)) & 1n) === 0n) tz++
  }
  const prefix = bits - tz
  const leadingOnes = n >> BigInt(tz)
  return leadingOnes === (1n << BigInt(prefix)) - 1n ? prefix : null
}

// IPv4Network._make_netmask: a decimal prefix length, else a dotted netmask,
// else a dotted hostmask.
function ipv4Prefix(mask: string): number | null {
  if (DECIMAL.test(mask)) {
    const n = Number(mask)
    if (n >= 0 && n <= 32) return n
  }
  const n = ipv4Int(mask)
  if (n === null) return null
  return prefixFromIpInt(n, 32) ?? prefixFromIpInt(n ^ 0xffffffffn, 32)
}

// IPv6Network._make_netmask: a decimal prefix length only.
function ipv6Prefix(mask: string): number | null {
  if (!DECIMAL.test(mask)) return null
  const n = Number(mask)
  return n >= 0 && n <= 128 ? n : null
}

// ipaddress.ip_network(str, strict=False): IPv4Network first, then IPv6Network.
function parseIpNetwork(s: string): ParsedNetwork | null {
  const pieces = s.split('/')
  if (pieces.length > 2) return null
  const [addr, mask] = pieces
  const v4 = ipv4Int(addr)
  if (v4 !== null) {
    const prefix = mask === undefined ? 32 : ipv4Prefix(mask)
    if (prefix !== null) {
      return { version: 4, value: v4 & maskOf(4, prefix), prefix, scope: null }
    }
  }
  const v6 = parseIpv6(addr)
  if (v6 === null) return null
  const prefix = mask === undefined ? 128 : ipv6Prefix(mask)
  if (prefix === null) return null
  const value = v6.value & maskOf(6, prefix)
  return { version: 6, value, prefix, scope: value === v6.value ? v6.scope : null }
}

const IPV4_MAPPED_PREFIX = 0xffffn

// The IPv4 value an IPv4-mapped IPv6 value (::ffff:a.b.c.d) wraps, else null
// (IPv6Address.ipv4_mapped).
function ipv4Mapped(version: IpVersion, value: bigint): bigint | null {
  return version === 6 && value >> 32n === IPV4_MAPPED_PREFIX ? value & 0xffffffffn : null
}

// network.canonical_ip on a parsed address.
function canonicalIp(ip: ParsedIp): ParsedIp {
  const v4 = ipv4Mapped(ip.version, ip.value)
  return v4 === null ? ip : { version: 4, value: v4, scope: null }
}

// One rule's ip as IpRuleIn stores it, then as network._rule_target reads the
// stored string back: the form rules_allow_ip matches it with. null for a rule
// that does not parse (the server refuses the whole save over it; the live
// filter would skip it).
function compileIpRule(raw: string): CompiledRule | null {
  const v = pyStrip(raw)
  if (v.includes('/')) {
    const net = parseIpNetwork(v)
    if (!net) return null
    if (net.prefix === maxPrefix(net.version)) {
      return { kind: 'host', ip: canonicalIp({ version: net.version, value: net.value, scope: net.scope }) }
    }
    // A prefix of 96 or more keeps the network inside ::ffff:0:0/96 exactly
    // when its network address is (net.subnet_of(_IPV4_MAPPED_NET)). Python's
    // subnet_of is False for a network that keeps a zone (%eth0), so such a
    // rule stays IPv6 there, and here.
    const v4 = net.prefix >= 96 && net.scope === null ? ipv4Mapped(net.version, net.value) : null
    if (v4 !== null) return { kind: 'net', version: 4, value: v4, prefix: net.prefix - 96 }
    return { kind: 'net', version: net.version, value: net.value, prefix: net.prefix }
  }
  const ip = parseIpAddress(v)
  return ip ? { kind: 'host', ip: canonicalIp(ip) } : null
}

function ruleMatches(rule: CompiledRule, ip: ParsedIp): boolean {
  if (rule.kind === 'host') {
    return rule.ip.version === ip.version && rule.ip.value === ip.value && rule.ip.scope === ip.scope
  }
  return rule.version === ip.version && (ip.value & maskOf(rule.version, rule.prefix)) === rule.value
}

// IPv4 127.0.0.0/8 and IPv6 ::1, on an address already in canonical form.
function isLoopback(ip: ParsedIp): boolean {
  if (ip.version === 4) return ip.value >> 24n === 127n
  return ip.value === 1n
}

// The client address as rules_allow_ip compares it: parsed, then unwrapped
// if IPv4-mapped. null when it does not parse.
function parseClientIp(clientIp: string): ParsedIp | null {
  const ip = parseIpAddress(clientIp)
  return ip && canonicalIp(ip)
}

export function isLoopbackIp(clientIp: string): boolean {
  const ip = parseClientIp(clientIp)
  return !!ip && isLoopback(ip)
}

// The first rule that matches `clientIp` (the one that decides for it, unless
// it is loopback), or undefined when none does or the IP does not parse.
// Like the server, every rule is considered, however long the list.
export function firstMatchingIpRule<R extends IpFilterRule>(
  clientIp: string,
  rules: readonly R[],
): R | undefined {
  const ip = parseClientIp(clientIp)
  if (!ip) return undefined
  return rules.find((r) => {
    const compiled = compileIpRule(r.ip)
    return !!compiled && ruleMatches(compiled, ip)
  })
}

// network.rules_allow_ip: would `clientIp` get through this rule set?
export function rulesAllowIp(
  clientIp: string,
  enabled: boolean,
  defaultPolicy: string,
  rules: readonly IpFilterRule[],
): boolean {
  const ip = parseClientIp(clientIp)
  if (ip && isLoopback(ip)) return true
  if (!enabled) return true
  if (!ip) return defaultPolicy === 'allow'
  const match = firstMatchingIpRule(clientIp, rules)
  return match ? match.action === 'allow' : defaultPolicy === 'allow'
}
