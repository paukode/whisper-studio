import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

export default defineConfig(({ command }) => {
  // setup.sh picks the backend port (41430, or the nearest free one) and
  // exports BACKEND_PORT for vite. Plain ``npm run dev`` uses 41430. Bind everything to 127.0.0.1 (not ``localhost``) so
  // we never go through the OS resolver — guest/captive Wi-Fi, IPv6
  // ``::1`` mismatches, and stale /etc/hosts entries all stop mattering.
  const backendPort = process.env.BACKEND_PORT || '41430';
  const backendHttp = `http://127.0.0.1:${backendPort}`;
  const backendWs = `ws://127.0.0.1:${backendPort}`;

  return {
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
    },
  },
  server: {
    host: '127.0.0.1',
    // Honor PORT when a harness (e.g. preview tooling) assigns one; plain
    // ``npm run dev`` uses the source install's fixed UI port.
    port: Number(process.env.PORT) || 41460,
    proxy: {
      '/api': backendHttp,
      '/static/modules': backendHttp,
      '/static/style.css': backendHttp,
      '/static/fonts': backendHttp,
      '/ws': {
        target: backendWs,
        ws: true,
      },
    },
    // Stop the dev server from watching worktree directories. When the
    // assistant runs `git worktree add` (via the enter_worktree tool or
    // any other path), the new directory appears inside the workspace
    // and chokidar would otherwise stream every file in as a change —
    // triggering an HMR cascade that ends with a full page reload (e.g.
    // tsconfig detection) and severs the live chat stream.
    //
    // We ignore these in-tree worktree locations:
    //  - `.whisper/worktrees/**`: where the enter_worktree tool puts them.
    //  - `.claude/worktrees/**`: where worktrees are created in this env.
    //  - `.worktrees/**`: where the ws_create_worktree tool puts them.
    // A sibling `<repo>-<branch>` worktree (the terminal panel's default)
    // is outside the vite root, so it is never watched.
    watch: {
      ignored: [
        '**/.whisper/worktrees/**',
        '**/.claude/worktrees/**',
        '**/.worktrees/**',
      ],
    },
  },
  // Only set base for production builds — dev server serves from /
  base: command === 'build' ? '/static/dist/' : '/',
  build: {
    outDir: 'static/dist',
    emptyOutDir: true,
    sourcemap: true,
    rollupOptions: {
      output: {
        manualChunks(id: string) {
          if (id.includes('node_modules/react-dom') || id.includes('node_modules/react/')) {
            return 'react';
          }
          if (id.includes('node_modules/zustand')) {
            return 'zustand';
          }
          if (id.includes('node_modules/@tanstack/react-query')) {
            return 'react-query';
          }
        },
      },
    },
  },
  test: {
    globals: true,
    environment: 'jsdom',
    setupFiles: ['./src/test/setup.ts'],
    include: ['src/**/*.test.{ts,tsx}'],
    css: true,
  },
  };
});
