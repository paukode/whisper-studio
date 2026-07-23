import { useEffect, useState } from 'react';

export interface UseBroadcastChannelReturn {
  isOtherTabOpen: boolean;
}

/**
 * Tab detection guard matching the existing BroadcastChannel protocol.
 *
 * Creates a BroadcastChannel named 'whisper_studio'. On mount, sends a
 * 'ping' message. If another tab is already open, it will respond with
 * 'pong', setting `isOtherTabOpen` to true. This tab also listens for
 * incoming 'ping' messages and responds with 'pong'.
 */
export function useBroadcastChannel(): UseBroadcastChannelReturn {
  const [isOtherTabOpen, setIsOtherTabOpen] = useState(false);

  useEffect(() => {
    // BroadcastChannel may not be available in all environments (e.g., tests)
    if (typeof BroadcastChannel === 'undefined') return;

    const channel = new BroadcastChannel('whisper_studio');

    const handleMessage = (event: MessageEvent) => {
      if (event.data === 'ping') {
        // Another tab is asking if anyone is here — respond
        channel.postMessage('pong');
      } else if (event.data === 'pong') {
        // Another tab responded to our ping
        setIsOtherTabOpen(true);
      }
    };

    channel.addEventListener('message', handleMessage);

    // Send a ping to detect existing tabs
    channel.postMessage('ping');

    return () => {
      channel.removeEventListener('message', handleMessage);
      channel.close();
    };
  }, []);

  return { isOtherTabOpen };
}
