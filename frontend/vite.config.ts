import path from 'path';
import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig({
  // 走内网网关时用 VITE_BASE_PATH 指定带前缀的静态资源根；默认 /static/ 保持直连行为。
  base: process.env.VITE_BASE_PATH || '/static/',
  plugins: [react()],
  resolve: {
    alias: {
      '@': path.resolve(__dirname, '.'),
    },
  },
  build: {
    outDir: process.env.VITE_OUT_DIR || '../static',
    sourcemap: false,
    rollupOptions: {
      output: {
        manualChunks: undefined,
      },
    },
    // static/ also contains uploads and backend-owned files.
    emptyOutDir: false,
  },
});
