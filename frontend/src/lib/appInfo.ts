declare const __APP_VERSION__: string

export const APP_INFO = {
  name: 'Tessera',
  version: __APP_VERSION__,
  description: 'An open-source WSI platform for learning histology and pathology',
  repoUrl: 'https://github.com/yosukey/tessera-wsi',
  license: 'AGPL-3.0',
  licenseUrl: 'https://www.gnu.org/licenses/agpl-3.0.html',
  copyright: '© 2026 Yosuke Yamazaki',
  paperUrl: '',
} as const
