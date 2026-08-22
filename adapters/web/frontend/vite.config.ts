/// <reference types="vitest" />
// This Vite configuration is added because Clonoth will later mount the built frontend under /web/.
// It wires React and Tailwind CSS v4 through Vite plugins while keeping the backend API disconnected.
// The test block explains how the skeleton is verified: Vitest runs React components in jsdom with a shared setup file.
// defineConfig comes from Vitest because Vite 6's base config type does not include the test field directly.
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  // 相对而非 /web/：多开时每个号挂在自己的路径前缀下，绝对 base 会让第二个号的
  // 资源请求全部落到第一个号那里。页面切换只改 query，pathname 不变，相对解析是安全的。
  base: './',
  plugins: [react(), tailwindcss()],
  test: {
    environment: 'jsdom',
    setupFiles: './src/setupTests.ts',
  },
});
