/**
 * The recording websocket's stop handshake as server/websocket.py runs it:
 * `stop` is answered with `session_ended` once the final decode and the
 * translation drain are done, then the server closes the socket. A test
 * socket calls this from its send() so recordingController.stop() settles
 * the way it does against the real server.
 */
interface ServerDrivenSocket {
  readyState: number;
  close(): void;
  _receiveMessage(data: object): void;
}

export function answerStopLikeServer(sock: ServerDrivenSocket, data: string | ArrayBuffer): void {
  if (typeof data !== 'string') return;
  let type: unknown;
  try {
    type = (JSON.parse(data) as { type?: unknown }).type;
  } catch {
    return;
  }
  if (type !== 'stop') return;
  setTimeout(() => {
    if (sock.readyState !== WebSocket.OPEN) return;
    sock._receiveMessage({ type: 'session_ended' });
    sock.close();
  }, 0);
}
