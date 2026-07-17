import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// Lane C frontend. Dev server on 5175 so it never collides with the
// Lane D backend (8130). API base + mock mode are controlled via env
// (see src/api.js): VITE_MOCK=1 (default) demos with no backend.
export default defineConfig({
  plugins: [react()],
  server: { port: 5175, strictPort: false },
})
