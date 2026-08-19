/// <reference types="vitest" />
// This Vite configuration is added because Clonoth will later mount the built frontend under /web/.
// It wires React and Tailwind CSS v4 through Vite plugins while keeping the backend API disconnected.
// The test block explains how the skeleton is verified: Vitest runs React components in jsdom with a shared setup file.
// defineConfig comes from Vitest because Vite 6's base config type does not include the test field directly.
import react from '@vitejs/plugin-react';
import tailwindcss from '@tailwindcss/vite';
import { defineConfig } from 'vitest/config';

export default defineConfig({
  base: '/web/',
  plugins: [react(), tailwindcss()],
  test: {
    environment: 'jsdom',
    setupFiles: './src/setupTests.ts',
  },
});
