import { describe, expect, it } from 'vitest'
import { MAX_IP_RULES, firstMatchingIpRule, isLoopbackIp, parseIpAddress, rulesAllowIp } from './ipFilter'

// Every expectation below is what services/app/wsi_app/network.rules_allow_ip
// returns for the same rules after schemas.IpRuleIn has canonicalised them
// and network._rule_target has read them back (checked against the Python
// implementation), so the dashboard's pre-save warning fires exactly when the
// server's self-lockout guard would refuse.

type Rule = { ip: string; action: 'allow' | 'deny' }
const allow = (ip: string): Rule => ({ ip, action: 'allow' })
const deny = (ip: string): Rule => ({ ip, action: 'deny' })
type Row = [client: string, rules: Rule[], defaultPolicy: 'allow' | 'deny', allowed: boolean]

const check = (rows: Row[]) => {
  for (const [client, rules, def, allowed] of rows) {
    expect(
      rulesAllowIp(client, true, def, rules),
      `${client} vs ${JSON.stringify(rules)} default ${def}`,
    ).toBe(allowed)
  }
}

describe('rulesAllowIp — IPv6 CIDRs', () => {
  it('denies an address inside a deny CIDR and lets one outside fall through', () => {
    check([
      ['2001:db8::5', [deny('2001:db8::/32')], 'allow', false],
      ['2001:db8::5', [deny('2001:db8::/64')], 'allow', false],
      ['2001:db9::5', [deny('2001:db8::/32')], 'allow', true],
      ['2001:db8:1::5', [deny('2001:db8::/48')], 'allow', true],
      ['2001:db8::5', [deny('::/0')], 'allow', false],
    ])
  })

  it('allows an address inside an allow CIDR under a deny default', () => {
    check([
      ['2001:db8::5', [allow('2001:db8::/32')], 'deny', true],
      ['2001:db9::5', [allow('2001:db8::/32')], 'deny', false],
    ])
  })

  it('compares addresses, not spellings, and masks host bits like strict=False', () => {
    check([
      ['2001:db8::5', [deny('2001:DB8:0:0::/32')], 'allow', false],
      ['2001:0db8:0000:0000:0000:0000:0000:0005', [deny('2001:db8::/32')], 'allow', false],
      ['2001:db8::5', [deny('2001:db8::1/32')], 'allow', false],
      ['2001:db8::5', [deny('2001:db8::5/128')], 'allow', false],
    ])
  })
})

describe('rulesAllowIp — rule order', () => {
  it('lets the first matching rule decide, whichever family the others are', () => {
    check([
      ['2001:db8::5', [allow('2001:db8::5'), deny('2001:db8::/32')], 'deny', true],
      ['2001:db8::5', [deny('2001:db8::/32'), allow('2001:db8::5')], 'allow', false],
      ['2001:db8::5', [allow('2001:db8::/64'), deny('2001:db8::/32')], 'deny', true],
      ['2001:db8::5', [deny('10.0.0.0/8'), allow('2001:db8::/32'), deny('::/0')], 'deny', true],
      ['10.1.2.3', [allow('::/0'), deny('10.0.0.0/8')], 'allow', false],
      ['10.1.2.3', [deny('2001:db8::/32'), allow('10.0.0.0/8')], 'deny', true],
    ])
  })

  it('reports the deciding rule', () => {
    const rules = [allow('10.0.0.0/8'), deny('2001:db8::/32'), allow('2001:db8::5')]
    expect(firstMatchingIpRule('2001:db8::5', rules)).toBe(rules[1])
    expect(firstMatchingIpRule('10.9.9.9', rules)).toBe(rules[0])
    expect(firstMatchingIpRule('192.0.2.1', rules)).toBeUndefined()
    expect(firstMatchingIpRule('not-an-ip', rules)).toBeUndefined()
  })
})

describe('rulesAllowIp — cases the server treats specially', () => {
  it('always allows loopback', () => {
    check([
      ['::1', [deny('::/0')], 'deny', true],
      ['0:0:0:0:0:0:0:1', [], 'deny', true],
      ['127.8.9.10', [deny('0.0.0.0/0')], 'deny', true],
      ['::', [], 'deny', false],
    ])
    expect(isLoopbackIp('128.0.0.1')).toBe(false)
  })

  // network.canonical_ip unwraps the client before the loopback check, so an
  // IPv4-mapped loopback is exempt on every supported Python (3.12.3 included).
  it('always allows an IPv4-mapped loopback', () => {
    check([
      ['::ffff:127.0.0.1', [deny('::/0')], 'deny', true],
      ['::ffff:7f00:1', [deny('::ffff:127.0.0.1')], 'deny', true],
      ['::ffff:127.0.0.1', [deny('0.0.0.0/0')], 'deny', true],
      ['::ffff:127.0.0.1', [], 'allow', true],
    ])
    expect(isLoopbackIp('::ffff:7f00:1')).toBe(true)
    expect(isLoopbackIp('::ffff:128.0.0.1')).toBe(false)
  })

  it('matches an IPv4-mapped client and IPv4-mapped rules as IPv4', () => {
    check([
      ['10.0.0.1', [deny('::ffff:10.0.0.1')], 'allow', false],
      ['::ffff:10.0.0.1', [deny('10.0.0.0/8')], 'allow', false],
      ['::ffff:10.0.0.1', [allow('10.0.0.1')], 'deny', true],
      ['::ffff:10.0.0.1', [deny('::ffff:a00:1')], 'allow', false],
      ['10.1.2.3', [deny('::ffff:10.0.0.0/104')], 'allow', false],
      ['::ffff:10.1.2.3', [deny('::ffff:10.0.0.0/104')], 'allow', false],
      ['11.0.0.1', [deny('::ffff:10.0.0.0/104')], 'allow', true],
      ['10.0.0.1', [deny('::ffff:10.0.0.1/128')], 'allow', false],
      ['203.0.113.5', [allow('::ffff:0:0/96')], 'deny', true],
      ['2001:db8::5', [allow('::ffff:0:0/96')], 'deny', false],
    ])
  })

  it('keeps IPv4 and IPv6 apart once mapped addresses are unwrapped', () => {
    check([
      ['2001:db8::5', [deny('0.0.0.0/0')], 'allow', true],
      ['::ffff:10.0.0.1', [deny('::/0')], 'allow', true],
      // Shorter than /96: not wholly inside ::ffff:0:0/96, so it stays IPv6.
      ['::ffff:10.0.0.1', [deny('::ffff:0:0/95')], 'allow', true],
      ['::fffe:0:1', [deny('::ffff:0:0/95')], 'allow', false],
      // Not an IPv4-mapped prefix (::a.b.c.d, IPv4-compatible): stays IPv6.
      ['10.0.0.1', [deny('::10.0.0.1')], 'allow', true],
    ])
  })

  it('reads only the first MAX_IP_RULES rules, as the server does', () => {
    const filler = Array.from({ length: MAX_IP_RULES }, (_, i) =>
      allow(`2001:db8::${(i + 1).toString(16)}`),
    )
    check([
      ['10.0.0.1', [...filler, deny('10.0.0.1')], 'allow', true],
      ['10.0.0.1', [...filler.slice(1), deny('10.0.0.1')], 'allow', false],
    ])
  })

  it('matches a zone in address equality but not in network membership', () => {
    check([
      ['fe80::1%eth0', [deny('fe80::/10')], 'allow', false],
      ['fe80::1%eth0', [deny('fe80::1')], 'allow', true],
      ['fe80::1', [deny('fe80::1%eth0/128')], 'allow', true],
      ['fe80::1%eth0', [deny('fe80::1%eth0')], 'allow', false],
    ])
  })

  it('accepts the IPv4 forms ip_network accepts', () => {
    check([
      ['10.1.2.3', [deny('10.0.0.0/255.0.0.0')], 'allow', false],
      ['10.1.2.3', [deny('10.0.0.0/0.255.255.255')], 'allow', false],
      ['10.1.2.3', [deny('10.0.0.0/08')], 'allow', false],
      ['10.1.2.3', [deny(' 10.1.2.3 ')], 'allow', false],
    ])
  })

  it('skips rules that do not parse and gives an unparseable IP the default', () => {
    check([
      ['10.1.2.3', [deny('010.1.2.3'), deny('10.0.0.0/33'), deny('10.0.0.0/+8'), deny('2001:db8::/0x20'), deny('1::2::3')], 'allow', true],
      ['testclient', [allow('0.0.0.0/0')], 'deny', false],
      ['testclient', [deny('0.0.0.0/0')], 'allow', true],
      ['', [], 'deny', false],
    ])
  })

  it('allows everything while the filter is disabled', () => {
    expect(rulesAllowIp('2001:db8::5', false, 'deny', [deny('::/0')])).toBe(true)
  })
})

describe('parseIpAddress', () => {
  it('parses like ipaddress.ip_address', () => {
    expect(parseIpAddress('::ffff:10.0.0.1')).toEqual({ version: 6, value: 0xffff0a000001n, scope: null })
    expect(parseIpAddress('fe80::1%eth0')).toMatchObject({ version: 6, scope: 'eth0' })
    for (const bad of ['10.0.0.01', '1.2.3', '12345::1', ':1:2:3:4:5:6:7', '1:2:3:4:5:6:7:8:9', 'fe80::1%', '10.0.0.1/32']) {
      expect(parseIpAddress(bad), bad).toBeNull()
    }
  })
})
