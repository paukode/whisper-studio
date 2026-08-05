/** 1.5 GB / 85 MB style, decimal units to match download-size conventions.
 *  Shared by Settings > Models and the pre-recording download banner. */
export const humanSize = (n: number | null | undefined): string => {
  if (!n || n <= 0) return '—';
  if (n >= 1e9) return `${(n / 1e9).toFixed(1)} GB`;
  if (n >= 1e6) return `${Math.round(n / 1e6)} MB`;
  return `${Math.max(1, Math.round(n / 1e3))} KB`;
};
