// @vitest-environment jsdom
import React from 'react'
import { cleanup, render, screen } from '@testing-library/react'
import { afterEach, describe, expect, it, vi } from 'vitest'
import type { ServerInfo } from '../../lib/api'

const api = vi.hoisted(() => ({ adminGetServerInfo: vi.fn(), adminGetStats: vi.fn() }))
vi.mock('../../lib/api', async () => {
  const actual = await vi.importActual<typeof import('../../lib/api')>('../../lib/api')
  return { ...actual, ...api }
})

import { I18nProvider } from '../../lib/i18n'
import AdminServerSection from './AdminServerSection'

function info(over: Partial<ServerInfo> = {}): ServerInfo {
  return {
    hostname: 'wsi-host',
    fqdn: null,
    interfaces: [],
    os: {
      system: 'Linux', release: '6.8', version: '#1', machine: 'x86_64',
      distro_pretty_name: 'Ubuntu', glibc_version: null, timezone: 'JST', tz_name: 'Asia/Tokyo',
      utc_offset: '+09:00', server_time: '2026-01-02T05:00:00+09:00', boot_time: null, virtualization: 'kvm',
    },
    dmi: { sys_vendor: null, product_name: null, product_version: null },
    cpu: {
      model: 'cpu', vendor: null, logical: 4, physical: 2, sockets: 1, freq_current_mhz: null,
      freq_max_mhz: null, usage_total_pct: null, usage_per_core_pct: null, temperature_c: null,
      cache: { l1d_kb: null, l1i_kb: null, l2_kb: null, l3_kb: null }, flags_subset: [],
    },
    memory: {
      total_mb: null, available_mb: null, used_mb: null, buffers_mb: null, cached_mb: null,
      swap_total_mb: null, swap_used_mb: null, swap_free_mb: null, hugepages_total: null, hugepages_size_kb: null,
    },
    load_avg: { m1: 0, m5: 0, m15: 0 },
    uptime_seconds: 60,
    app_process: {
      pid: 12, threads: 4, fds: 10, rss_mb: 100, vm_peak_mb: null, vm_size_mb: null, started_at: null,
      app_version: '1.0.0', app_version_source: 'VERSION', db_app_version: null, python_version: '3.12',
      workers: {
        count: 2, rss_mb_total: 300,
        items: [
          { pid: 11, rss_mb: 200, threads: 5, current: false },
          { pid: 12, rss_mb: 100, threads: 4, current: true },
        ],
      },
    },
    system_processes: { process_count: 3 },
    db: { kind: 'sqlite', version: '3.45' },
    disk_data_dir: {
      path: '/data', total_gb: null, used_gb: null, free_gb: null, used_pct: null,
      fstype: null, inodes_total: null, inodes_free: null, rotational: null,
    },
    other_mounts: [],
    disk_io_total: null,
    cgroup: {
      in_container: false, container_kind: null, cgroup_path: '/system.slice/wsi-app.service',
      memory_max_bytes: 2 * 1024 ** 3, cpu_quota_us: null, cpu_period_us: null,
    },
    dzi_cache: { entries: 0, max: 8, bytes: 0, max_bytes: 0 },
    nginx_cache: {
      path: '/var/cache/nginx/wsi_dzi', max_size: '50g', inactive: '30d', used_bytes: 1024, file_count: 3,
      source: 'stat_file', partial: false, stat_updated_at: '2026-01-02T04:00:00+09:00',
      stat_age_seconds: 3600, stat_stale: true,
    },
    app_update: {
      enabled: false, current: '1.0.0', current_source: 'VERSION', latest: null,
      update_available: false, comparable: false, checked_at: null, last_attempt_at: null,
    },
    ...over,
  }
}

afterEach(() => {
  cleanup()
  vi.resetAllMocks()
})

function mount() {
  return render(<I18nProvider><AdminServerSection /></I18nProvider>)
}

describe('AdminServerSection', () => {
  it('shows unreadable memory and disk figures as unknown, not 0', async () => {
    api.adminGetServerInfo.mockResolvedValue(info())
    api.adminGetStats.mockReturnValue(new Promise(() => {}))
    mount()
    await screen.findByText('wsi-host')
    expect(screen.queryByText('0 MB')).toBeNull()
    expect(screen.queryByText(/0 \/ 0 MB/)).toBeNull()
    expect(screen.queryByText('0 GB')).toBeNull()
  })

  it('lists every worker and marks the one that answered', async () => {
    api.adminGetServerInfo.mockResolvedValue(info())
    api.adminGetStats.mockReturnValue(new Promise(() => {}))
    mount()
    expect(await screen.findByText('Workers (2)')).toBeTruthy()
    expect(screen.getByText('300 MB')).toBeTruthy()
    expect(screen.getByText('(this request)')).toBeTruthy()
  })

  it("shows the service unit's cgroup limits on a plain host", async () => {
    api.adminGetServerInfo.mockResolvedValue(info())
    api.adminGetStats.mockReturnValue(new Promise(() => {}))
    mount()
    expect(await screen.findByText('/system.slice/wsi-app.service')).toBeTruthy()
    expect(screen.getByText('2.00 GB')).toBeTruthy()
  })

  it('warns when the cache stat file has gone stale, and says when the page was fetched', async () => {
    api.adminGetServerInfo.mockResolvedValue(info())
    api.adminGetStats.mockReturnValue(new Promise(() => {}))
    mount()
    expect(await screen.findByText(/Not updated for 1h 0m/)).toBeTruthy()
    expect(screen.getByText(/As of: /)).toBeTruthy()
  })
})
