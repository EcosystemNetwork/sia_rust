export class AquariumWsClient {
  constructor(aquariumId, onMessage) {
    this._id      = aquariumId;
    this._onMsg   = onMessage;
    this._ws      = null;
    this._delay   = 1000;
    this._stopped = false;
  }
  connect() {
    if (this._stopped) return;
    // Served under SIA Studio: tanks live at /ws/aquarium/{id}. An optional
    // window.SIA_API_BASE (e.g. a split frontend/backend deploy) overrides host.
    const base = (typeof window !== 'undefined' && window.SIA_API_BASE) || '';
    let url;
    if (base) {
      url = base.replace(/^http/, 'ws').replace(/\/$/, '') + `/ws/aquarium/${this._id}`;
    } else {
      const proto = location.protocol === 'https:' ? 'wss' : 'ws';
      url = `${proto}://${location.host}/ws/aquarium/${this._id}`;
    }
    const ws    = new WebSocket(url);
    this._ws    = ws;
    ws.onopen    = () => { this._delay = 1000; };
    ws.onmessage = (ev) => {
      try { this._onMsg(JSON.parse(ev.data)); } catch {}
    };
    ws.onclose   = () => {
      if (this._stopped) return;
      setTimeout(() => { this._delay = Math.min(this._delay*1.5,15000); this.connect(); }, this._delay);
    };
    ws.onerror   = () => ws.close();
  }
  stop() { this._stopped = true; this._ws?.close(); }
}
