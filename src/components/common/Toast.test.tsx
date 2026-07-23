import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { Toast } from './Toast';

describe('Toast', () => {
  it('renders an action link opening in a new tab when provided', () => {
    render(
      <Toast
        id="t1"
        type="info"
        message='Tool "aws_boto3" produced ~53 KB; a trimmed ~47 KB (head+tail) was sent to the model.'
        count={1}
        action={{ label: 'View full output', href: '/api/result-cache/aws_boto3_1.txt' }}
        onClose={vi.fn()}
      />
    );
    const link = screen.getByRole('link', { name: 'View full output' });
    expect(link).toHaveAttribute('href', '/api/result-cache/aws_boto3_1.txt');
    expect(link).toHaveAttribute('target', '_blank');
  });

  it('renders no action link by default', () => {
    render(<Toast id="t2" type="info" message="plain toast" count={1} onClose={vi.fn()} />);
    expect(screen.queryByRole('link')).toBeNull();
  });
});
