import { defineConfig, mergeConfig } from 'vitest/config'
import viteConfig from './vite.config'

// Unit / hook tests (`npm test`). Shares vite.config's plugins and `define`
// (e.g. __APP_VERSION__) so modules under test compile exactly as in the app.
// Pure-logic tests run in Node; a test that needs the DOM opts in per file
// with a `// @vitest-environment jsdom` docblock, which keeps the bulk of the
// suite free of jsdom's start-up cost.
export default mergeConfig(viteConfig, defineConfig({
  test: {
    include: ['src/**/*.test.{ts,tsx}'],
    environment: 'node',
    setupFiles: ['src/test/setup.ts'],
    restoreMocks: true,
  },
}))
