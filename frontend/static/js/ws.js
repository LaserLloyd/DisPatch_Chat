// WebSocket client with auto-reconnect, heartbeat, and liveness watchdog.
// URL is derived from window.location so it works over LAN / Tailscale too.

const HEARTBEAT_MS = 30000;

export class ChatSocket {
  constructor({ onMessage, onStatus, onReconnect }) {
    this.onMessage = onMessage;
    this.onStatus = onStatus || (() => {});
    this.onReconnect = onReconnect || (() => {});
    this.ws = null;
    this.backoff = 500;
    this.maxBackoff = 8000;
    this.heartbeat = null;
    this.reconnectTimer = null;  // so a reconnect can be cancelled, and never doubled
    this.closedByUser = false;
    this.hasConnected = false;   // distinguishes first connect from reconnects
    this.lastAlive = 0;          // last time we heard ANYTHING from the server
  }

  url() {
    const proto = location.protocol === 'https:' ? 'wss' : 'ws';
    return `${proto}://${location.host}/ws`;
  }

  connect() {
    this.closedByUser = false;
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    // Every handler below closes over `sock`, the socket it was registered on,
    // and bails when `this.ws` has since moved on. Reading `this.ws` inside a
    // handler asks "what is the CURRENT socket", not "which socket fired me" —
    // so a late close/error from a superseded connection would tear down the
    // fresh one (close() it, clear its heartbeat, report offline) and schedule
    // a second reconnect on top of the one already in flight. Nothing reaches
    // that today because connect() is the only place this.ws is assigned and
    // it is never called with a live socket open; the guard is here so that
    // stays a property of the class rather than of its callers.
    let sock;
    try {
      sock = new WebSocket(this.url());
      this.ws = sock;
    } catch {
      this.scheduleReconnect();
      return;
    }

    sock.addEventListener('open', () => {
      if (this.ws !== sock) { try { sock.close(); } catch {} return; }
      this.onStatus(true);
      const reconnected = this.hasConnected;
      this.hasConnected = true;
      this.lastAlive = Date.now();
      clearInterval(this.heartbeat);
      // Heartbeat doubles as a liveness watchdog: on a silent network drop
      // (common on mobile/Tailscale) readyState stays OPEN and send() keeps
      // "succeeding" while messages go nowhere. If nothing has been heard for
      // >2 heartbeats, force-close to trigger the reconnect + resync path.
      this.heartbeat = setInterval(() => {
        if (this.ws !== sock) { clearInterval(this.heartbeat); return; }
        if (Date.now() - this.lastAlive > HEARTBEAT_MS * 2 + 5000) {
          try { sock.close(); } catch {}
          return;
        }
        this.send({ type: 'ping' });
      }, HEARTBEAT_MS);
      if (reconnected) this.onReconnect();  // re-sync state after a drop
    });

    sock.addEventListener('message', (e) => {
      if (this.ws !== sock) return;
      this.lastAlive = Date.now();
      this.backoff = 500;   // a real message proves the link works
      let data;
      try { data = JSON.parse(e.data); } catch { return; }
      this.onMessage(data);
    });

    sock.addEventListener('close', () => {
      if (this.ws !== sock) return;   // a superseded socket closing is not our drop
      clearInterval(this.heartbeat);
      this.onStatus(false);
      if (!this.closedByUser) this.scheduleReconnect();
    });

    sock.addEventListener('error', () => { try { sock.close(); } catch {} });
  }

  scheduleReconnect() {
    if (this.closedByUser) return;   // locked / stopped — don't come back
    // One timer at a time: two close-ish events (close after error is the usual
    // pair) would otherwise arm two, and both would call connect() — the second
    // orphaning the socket the first had just made.
    if (this.reconnectTimer) return;
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      if (!this.closedByUser) this.connect();
    }, this.backoff);
    this.backoff = Math.min(this.backoff * 1.7, this.maxBackoff);
  }

  // Stop for good (used when the app locks): no reconnect until connect() again.
  stop() {
    this.closedByUser = true;
    clearInterval(this.heartbeat);
    clearTimeout(this.reconnectTimer);
    this.reconnectTimer = null;
    // this.ws is deliberately NOT nulled: the close handler's `this.ws !== sock`
    // guard would then treat this socket's own close as a stale event and skip
    // onStatus(false), leaving the connection dot green after a lock.
    try { if (this.ws) this.ws.close(); } catch {}
  }

  send(obj) {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) {
      this.ws.send(JSON.stringify(obj));
      return true;
    }
    return false;
  }
}
