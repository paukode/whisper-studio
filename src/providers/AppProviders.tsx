import React from 'react';
import { ThemeProvider } from '@/providers/ThemeProvider';
import { QueryProvider } from '@/providers/QueryProvider';

/**
 * AppProviders composes all context providers for the application.
 * Wraps children in ThemeProvider and TanStack QueryProvider.
 */
const AppProviders: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  return (
    <QueryProvider>
      <ThemeProvider>
        {children}
      </ThemeProvider>
    </QueryProvider>
  );
};

export default AppProviders;
