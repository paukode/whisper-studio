import React, { useMemo } from 'react';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';

export interface NotebookViewerProps {
  /** Path to the Jupyter notebook file. */
  filePath: string;
  /** Raw .ipynb file contents (JSON string). */
  content: string;
}

interface NotebookCell {
  cell_type: 'code' | 'markdown' | 'raw' | string;
  source: string | string[];
  outputs?: NotebookOutput[];
  execution_count?: number | null;
}

interface NotebookOutput {
  output_type: 'stream' | 'execute_result' | 'display_data' | 'error' | string;
  text?: string | string[];
  data?: Record<string, string | string[]>;
  ename?: string;
  evalue?: string;
  traceback?: string[];
}

interface ParsedNotebook {
  cells: NotebookCell[];
  language: string;
  error?: string;
}

function joinSource(src: string | string[] | undefined): string {
  if (!src) return '';
  return Array.isArray(src) ? src.join('') : src;
}

function parseNotebook(raw: string): ParsedNotebook {
  try {
    const data = JSON.parse(raw) as {
      cells?: NotebookCell[];
      metadata?: { kernelspec?: { language?: string }; language_info?: { name?: string } };
    };
    const language =
      data.metadata?.language_info?.name ??
      data.metadata?.kernelspec?.language ??
      'python';
    return { cells: Array.isArray(data.cells) ? data.cells : [], language };
  } catch (e) {
    return { cells: [], language: 'python', error: e instanceof Error ? e.message : String(e) };
  }
}

function renderOutput(output: NotebookOutput, idx: number): React.ReactNode {
  // Plain stdout/stderr
  if (output.output_type === 'stream') {
    return (
      <pre key={idx} className="notebook-output notebook-output-stream">
        {joinSource(output.text)}
      </pre>
    );
  }
  // Errors
  if (output.output_type === 'error') {
    const trace = (output.traceback ?? []).join('\n');
    return (
      <pre key={idx} className="notebook-output notebook-output-error">
        {output.ename}: {output.evalue}
        {trace ? '\n' + trace : ''}
      </pre>
    );
  }
  // Rich result / display data — prefer text/plain, fall back to image/png
  if (output.data) {
    const text = output.data['text/plain'];
    const png = output.data['image/png'];
    return (
      <div key={idx} className="notebook-output notebook-output-result">
        {png ? (
          <img
            src={`data:image/png;base64,${Array.isArray(png) ? png.join('') : png}`}
            alt="cell output"
          />
        ) : null}
        {text ? <pre>{Array.isArray(text) ? text.join('') : text}</pre> : null}
      </div>
    );
  }
  return null;
}

/**
 * Renders a Jupyter `.ipynb` document by parsing the JSON and emitting one
 * block per cell — markdown cells go through MarkdownRenderer, code cells get
 * a syntax-styled `<pre>` plus their captured outputs.
 *
 * The renderer is read-only — execution lives on the backend (server/notebook.py).
 */
export const NotebookViewer: React.FC<NotebookViewerProps> = ({ filePath, content }) => {
  const fileName = filePath.split('/').pop() ?? filePath;
  const parsed = useMemo(() => parseNotebook(content), [content]);

  if (parsed.error) {
    return (
      <div className="notebook-viewer" role="document" aria-label={`Notebook: ${fileName}`}>
        <div className="notebook-viewer-header">
          <span className="notebook-viewer-filename">{fileName}</span>
        </div>
        <div className="notebook-viewer-content">
          <p>📓 Could not parse notebook JSON: {parsed.error}</p>
        </div>
      </div>
    );
  }

  return (
    <div className="notebook-viewer" role="document" aria-label={`Notebook: ${fileName}`}>
      <div className="notebook-viewer-header">
        <span className="notebook-viewer-filename">{fileName}</span>
        <span className="notebook-viewer-meta">
          {parsed.cells.length} cell{parsed.cells.length === 1 ? '' : 's'} · {parsed.language}
        </span>
      </div>
      <div className="notebook-viewer-content">
        {parsed.cells.map((cell, i) => {
          const source = joinSource(cell.source);
          if (cell.cell_type === 'markdown') {
            return (
              <div key={i} className="notebook-cell notebook-cell-markdown">
                <MarkdownRenderer content={source} />
              </div>
            );
          }
          if (cell.cell_type === 'code') {
            return (
              <div key={i} className="notebook-cell notebook-cell-code">
                <div className="notebook-cell-prompt">
                  In [{cell.execution_count ?? ' '}]:
                </div>
                <pre className="notebook-cell-source">{source}</pre>
                {(cell.outputs ?? []).map(renderOutput)}
              </div>
            );
          }
          // raw or unknown cell types — show as plain pre
          return (
            <div key={i} className="notebook-cell notebook-cell-raw">
              <pre>{source}</pre>
            </div>
          );
        })}
      </div>
    </div>
  );
};
