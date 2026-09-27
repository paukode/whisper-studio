import React from 'react';
import type { UsageTotals } from '@/types/costs';
import { formatTokenCount } from '@/components/chat/TokenCounter';
import { fmtCount, fmtFraction, fmtMoney } from './usageView';

interface TileProps {
  id: string;
  name: string;
  value: string;
  /** The exact figure behind a compact value, on hover. */
  exact?: string;
  estimated?: boolean;
  hero?: boolean;
  children?: React.ReactNode;
}

function Tile({ id, name, value, exact, estimated, hero, children }: TileProps) {
  return (
    <div
      className={`usage-tile${hero ? ' usage-tile--hero' : ''}${estimated ? ' usage-est' : ''}`}
      id={id}
    >
      <div className="usage-tile-name">{name}</div>
      <div className="usage-tile-value" title={exact}>
        {value}
      </div>
      {children && <div className="usage-tile-desc">{children}</div>}
    </div>
  );
}

/** Summary figures for the selected range: the estimated cost large, the
 *  token, cache and call figures beside it. Any tile whose number rests on
 *  estimated token counts is drawn as estimated, and the cost says how many
 *  calls. */
export const UsageTiles: React.FC<{ totals: UsageTotals }> = ({ totals }) => {
  const est = totals.estimated_calls > 0;
  const saving = totals.cache_savings_usd;

  return (
    <div className="usage-tiles">
      <Tile id="costsTileCost" name="Estimated cost" value={fmtMoney(totals.cost_usd)} estimated={est} hero>
        at list rates
        {est && (
          <>
            <br />
            <span className="usage-est-note">
              includes {fmtCount(totals.estimated_calls)} of {fmtCount(totals.calls)} calls with
              estimated token counts
            </span>
          </>
        )}
      </Tile>
      <div className="usage-tile-grid">
        <Tile
          id="costsTilePrompt"
          name="Prompt tokens"
          value={formatTokenCount(totals.prompt_tokens)}
          exact={fmtCount(totals.prompt_tokens)}
          estimated={est}
        >
          uncached input, cache reads and cache writes
        </Tile>
        <Tile
          id="costsTileOutput"
          name="Output tokens"
          value={formatTokenCount(totals.output_tokens)}
          exact={fmtCount(totals.output_tokens)}
          estimated={est}
        />
        {/* The hit rate divides by prompt tokens, so estimated prompts make it an estimate too. */}
        <Tile
          id="costsTileCache"
          name="Cache hit rate"
          value={fmtFraction(totals.cache_hit_rate)}
          exact={`${fmtCount(totals.cache_read_tokens)} tokens read from cache (counted on every model call)`}
          estimated={est}
        >
          {saving >= 0
            ? `about ${fmtMoney(saving)} less than uncached, at list rates`
            : `about ${fmtMoney(-saving)} more than uncached (cache writes cost more than the reads saved), at list rates`}
        </Tile>
        <Tile id="costsTileCalls" name="Model calls" value={fmtCount(totals.calls)}>
          {est
            ? `${fmtCount(totals.estimated_calls)} with estimated token counts`
            : totals.calls > 0 && 'every token count reported by the provider'}
        </Tile>
      </div>
    </div>
  );
};
