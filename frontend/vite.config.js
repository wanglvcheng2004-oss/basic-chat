import { defineConfig } from 'vite'
import vue from '@vitejs/plugin-vue'

export default defineConfig({
  plugins: [vue()],
  server: {
    port: 5173,
    proxy: {
      '/ws': { target: 'ws://127.0.0.1:8000', ws: true },
      '/health': { target: 'http://127.0.0.1:8000' },
      '/api': { target: 'http://127.0.0.1:8000' },
    },
  },
})
