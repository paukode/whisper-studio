import tseslint from 'typescript-eslint';
import reactHooks from 'eslint-plugin-react-hooks';

export default tseslint.config(
  {
    ignores: ['static/dist/**', 'node_modules/**'],
  },
  ...tseslint.configs.recommended,
  {
    plugins: {
      'react-hooks': reactHooks,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      // Honour the leading-underscore convention for intentionally-unused
      // bindings (function args kept to satisfy a signature, caught errors
      // we don't inspect, destructured rest-siblings). Without this the
      // default no-unused-vars flags every `_arg`, which the codebase uses
      // deliberately.
      '@typescript-eslint/no-unused-vars': ['error', {
        argsIgnorePattern: '^_',
        varsIgnorePattern: '^_',
        caughtErrorsIgnorePattern: '^_',
        ignoreRestSiblings: true,
      }],
      // The react-hooks "recommended" set bundles the React Compiler rules.
      // A dedicated hooks-hardening pass migrated every pre-existing violation
      // to its canonical fix (data fetches → useQuery, latest-value refs →
      // useEffect, derived state → useMemo, prop-sync resets → during-render
      // previous-value pattern), so these are now enforced as errors to keep
      // the codebase from regressing.
      'react-hooks/set-state-in-effect': 'error',
      'react-hooks/refs': 'error',
      'react-hooks/preserve-manual-memoization': 'error',
      'react-hooks/immutability': 'error',
    },
  },
);
