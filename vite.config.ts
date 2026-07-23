import { defineConfig } from 'vite';
import react from '@vitejs/plugin-react';
import { resolve } from 'path';

export default defineConfig(({ command }) => {
  // The backend port is dynamic: setup.sh starts at 8000 and increments
  // until it finds a free port, then exports BACKEND_PORT for vite. We
  // fall back to 8000 so plain ``npm run dev`` still works without the
  // setup script. Bind everything to 127.0.0.1 (not ``localhost``) so
  // we never go through the OS resolver — guest/captive Wi-Fi, IPv6
  // ``::1`` mismatches, and stale /etc/hosts entries all stop mattering.
  const backendPort = process.env.BACKEND_PORT || '8000';
  const backendHttp = `http://127.0.0.1:${backendPort}`;
  const backendWs = `ws://127.0.0.1:${backendPort}`;

  return {
  plugins: [react()],
  resolve: {
    alias: {
      '@': resolve(__dirname, 'src'),
      '@components': resolve(__dirname, 'src/components'),
      '@hooks': resolve(__dirname, 'src/hooks'),
      '@stores': resolve(__dirname, 'src/stores'),
      '@types': resolve(__dirname, 'src/types'),
      '@utils': resolve(__dirname, 'src/utils'),
    },
  },
  server: {
    host: '127.0.0.1',
    // Honor PORT when a harness (e.g. preview tooling) assigns one; fall
    // back to vite's default 5173 for plain ``npm run dev``.
    port: Number(process.env.PORT) || 5173,
    proxy: {
      '/api': backendHttp,
      '/static/modules': backendHttp,
      '/static/style.css': backendHttp,
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
    //  - `.whisper/worktrees/**` — where the enter_worktree tool puts them.
    //  - `.claude/worktrees/**` — where worktrees are created in this env.
    //  - `.worktrees/**` — the historical/manual location some flows use.
    //  - any worktree placed sibling-style at the repo root via
    //    `<repo>-<branch>` (legacy `addGitWorktree` default path); we
    //    can't pattern-match that from inside vite, so prefer the
    //    in-tree patterns and steer the assistant toward them via the
    //    git instructions prompt.
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
    coverage: {
      provider: 'v8',
      reporter: ['text', 'lcov', 'html'],
      include: ['src/**/*.{ts,tsx}'],
      exclude: [
        'src/**/*.test.{ts,tsx}',
        'src/test/**',
        'src/main.tsx',
        'src/**/*.d.ts',
      ],
    },
  },
  };
});
