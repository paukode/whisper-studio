import React from 'react';

export interface ErrorBoundaryProps {
  /** Optional fallback UI to render when an error is caught. */
  fallback?: React.ReactNode;
  /** Optional label for identifying which boundary caught the error. */
  label?: string;
  children: React.ReactNode;
}

interface ErrorBoundaryState {
  hasError: boolean;
  error: Error | null;
}

/**
 * React error boundary component.
 *
 * Catches JavaScript errors in its child component tree, logs them,
 * and renders a fallback UI instead of crashing the entire app.
 *
 * Requirements: 2.6, 14.5
 */
export class ErrorBoundary extends React.Component<ErrorBoundaryProps, ErrorBoundaryState> {
  constructor(props: ErrorBoundaryProps) {
    super(props);
    this.state = { hasError: false, error: null };
  }

  static getDerivedStateFromError(error: Error): ErrorBoundaryState {
    return { hasError: true, error };
  }

  componentDidCatch(error: Error, errorInfo: React.ErrorInfo): void {
    const label = this.props.label ?? 'ErrorBoundary';
    console.error(`[${label}] Caught error:`, error, errorInfo);
  }

  handleReset = (): void => {
    this.setState({ hasError: false, error: null });
  };

  render(): React.ReactNode {
    if (this.state.hasError) {
      if (this.props.fallback) {
        return this.props.fallback;
      }

      const label = this.props.label ?? 'component';

      return (
        <div className="error-boundary-fallback" role="alert">
          <h2>Something went wrong</h2>
          <p>
            An error occurred in the {label}. You can try reloading the page or
            resetting this section.
          </p>
          {this.state.error && (
            <details>
              <summary>Error details</summary>
              <pre>{this.state.error.message}</pre>
            </details>
          )}
          <button
            className="error-boundary-reset-btn"
            onClick={this.handleReset}
            type="button"
          >
            Try again
          </button>
        </div>
      );
    }

    return this.props.children;
  }
}
