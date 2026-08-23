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
    try {
      this.ws = new WebSocket(this.url());
    } catch {
      this.scheduleReconnect();
      return;
    }

    this.ws.addEventListener('open', () => {
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
        if (Date.now() - this.lastAlive > HEARTBEAT_MS * 2 + 5000) {
          try { this.ws.close(); } catch {}
          return;
        }
        this.send({ type: 'ping' });
      }, HEARTBEAT_MS);
      if (reconnected) this.onReconnect();  // re-sync state after a drop
    });

    this.ws.addEventListener('message', (e) => {
      this.lastAlive = Date.now();
      this.backoff = 500;   // a real message proves the link works
      let data;
      try { data = JSON.parse(e.data); } catch { return; }
      this.onMessage(data);
    });

    this.ws.addEventListener('close', () => {
      clearInterval(this.heartbeat);
      this.onStatus(false);
      if (!this.closedByUser) this.scheduleReconnect();
    });

    this.ws.addEventListener('error', () => { try { this.ws.close(); } catch {} });
  }

  scheduleReconnect() {
    if (this.closedByUser) return;   // locked / stopped — don't come back
    setTimeout(() => { if (!this.closedByUser) this.connect(); }, this.backoff);
    this.backoff = Math.min(this.backoff * 1.7, this.maxBackoff);
  }

  // Stop for good (used when the app locks): no reconnect until connect() again.
  stop() {
    this.closedByUser = true;
    clearInterval(this.heartbeat);
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
