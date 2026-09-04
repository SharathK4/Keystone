import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The backend is the source of truth for every number on the page, so the dev
// server proxies straight to it rather than the frontend holding a base URL and
// a CORS story. `LCE_API` overrides the target when the API runs elsewhere.
const target = process.env.LCE_API ?? 'http://127.0.0.1:8000'

export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': { target, changeOrigin: true },
    },
  },
})
