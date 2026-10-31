import { describe, expect, it } from 'vitest'
import { firstMatchingIpRule, isLoopbackIp, parseIpAddress, rulesAllowIp } from './ipFilter'

// Every expectation below is what services/app/wsi_app/network.rules_allow_ip
// returns for the same rules after schemas.IpRuleIn has canonicalised them
// (checked against the Python implementation), so the dashboard's pre-save
// warning fires exactly when the server's self-lockout guard would refuse.

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

  // ::ffff:127.x is_loopback is True on Python 3.13+ but False on Ubuntu
  // 24.04's 3.12.3, where the server's guard lets the rules decide. The
  // prediction must not call a save lockout-free that 3.12.3 would refuse.
  it('lets the rules decide for an IPv4-mapped loopback, as Python 3.12 does', () => {
    check([
      ['::ffff:127.0.0.1', [deny('::/0')], 'deny', false],
      ['::ffff:7f00:1', [deny('::ffff:127.0.0.1')], 'allow', false],
      ['::ffff:127.0.0.1', [], 'allow', true],
      ['::ffff:127.0.0.1', [allow('::ffff:127.0.0.0/104')], 'deny', true],
    ])
    expect(isLoopbackIp('::ffff:7f00:1')).toBe(false)
  })

  it('never matches IPv4 against IPv6, so a mapped address needs an IPv6 rule', () => {
    check([
      ['2001:db8::5', [deny('0.0.0.0/0')], 'allow', true],
      ['::ffff:10.0.0.1', [deny('10.0.0.0/8')], 'allow', true],
      ['::ffff:10.0.0.1', [deny('::ffff:10.0.0.0/104')], 'allow', false],
      ['::ffff:10.0.0.1', [deny('::ffff:a00:1')], 'allow', false],
      ['10.0.0.1', [deny('::ffff:10.0.0.1')], 'allow', true],
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
