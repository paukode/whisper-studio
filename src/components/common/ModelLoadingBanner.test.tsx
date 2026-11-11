import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ModelLoadingBanner } from './ModelLoadingBanner';
import { useUIStore } from '@/stores/uiStore';

describe('ModelLoadingBanner', () => {
  beforeEach(() => {
    useUIStore.setState({ modelLoading: null });
  });

  it('renders nothing when idle', () => {
    const { container } = render(<ModelLoadingBanner />);
    expect(container).toBeEmptyDOMElement();
  });

  it('shows download progress with the detail clause and a working Cancel', () => {
    let cancelled = false;
    useUIStore.getState().openModelBanner('speech-models').show({
      label: 'Parakeet TDT 0.6B v3',
      progress: 0.42,
      stage: 'downloading',
      detail: '1.1 GB of 2.5 GB · model 1 of 2',
      onCancel: () => {
        cancelled = true;
      },
    });
    render(<ModelLoadingBanner />);
    expect(
      screen.getByText('Downloading Parakeet TDT 0.6B v3 · 1.1 GB of 2.5 GB · model 1 of 2'),
    ).toBeInTheDocument();
    expect(screen.getByText('42%')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(cancelled).toBe(true);
  });

  it('renders the error stage with the message and a Dismiss button', () => {
    const flow = useUIStore.getState().openModelBanner('speech-models');
    flow.show({
      label: 'Parakeet TDT 0.6B v3',
      progress: 0,
      stage: 'error',
      detail: 'disk full',
      onCancel: flow.clear,
    });
    render(<ModelLoadingBanner />);
    expect(screen.getByRole('alert')).toBeInTheDocument();
    expect(screen.getByText('Download failed: Parakeet TDT 0.6B v3')).toBeInTheDocument();
    expect(screen.getByText('disk full')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Dismiss' }));
    expect(useUIStore.getState().modelLoading).toBeNull();
  });
});
