import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import App from '@/App';

describe('App', () => {
  it('renders the application shell', () => {
    render(<App />);
    // The empty-state welcome heading is action-oriented now (the redundant
    // "Whisper Studio" title was removed — the header already carries it).
    expect(screen.getAllByText('What can I help with?').length).toBeGreaterThanOrEqual(1);
  });
});
