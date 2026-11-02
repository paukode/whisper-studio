import { render, screen } from '@testing-library/react';
import { describe, it, expect } from 'vitest';
import { CommandPreview } from './CommandPreview';

describe('CommandPreview', () => {
  it('shows a failed command preview on the card, not a blank body', () => {
    render(<CommandPreview previewError="Command preview unavailable: bad input" />);
    expect(screen.getByRole('alert').textContent).toBe('Command preview unavailable: bad input');
  });

  it('renders the command when there is one', () => {
    render(<CommandPreview command="git status" cwd="/ws" />);
    expect(screen.getByText('$ git status')).toBeTruthy();
    expect(screen.queryByRole('alert')).toBeNull();
  });
});
