import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'
import tailwindcss from '@tailwindcss/vite'
import path from 'path'
import fs from 'fs'

function swVersionPlugin() {
  return {
    name: 'sw-version',
    closeBundle() {
      const swPath = path.resolve(__dirname, '../oap_agent/static/sw.js')
      if (!fs.existsSync(swPath)) return
      const version = `manifest-${Date.now()}`
      const content = fs.readFileSync(swPath, 'utf-8').replace('__SW_CACHE_VERSION__', version)
      fs.writeFileSync(swPath, content)
    },
  }
}

export default defineConfig({
  plugins: [react(), tailwindcss(), swVersionPlugin()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, 'src'),
    },
  },
  build: {
    outDir: '../oap_agent/static',
    emptyOutDir: true,
  },
  server: {
    proxy: {
      '/v1/agent': 'http://localhost:8303',
    },
  },
})
