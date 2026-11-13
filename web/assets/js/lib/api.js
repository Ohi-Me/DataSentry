/* API client, cache and the live socket.
 *
 * The cache is patched on WebSocket messages rather than invalidated. During
 * an incident burst, "invalidate and refetch" turns one server event into a
 * refetch storm from every open browser — the console falling over during
 * exactly the outage it exists to show you. Patching keeps the screen honest
 * and the network quiet.
 *
 * Reconnect is exponential with a cap, and on reconnect we fetch a delta
 * rather than reloading the world.
 */

const BASE = "/api/v1";

class Api {
  constructor() {
    this.cache = new Map();
    this.inflight = new Map();
    this.listeners = new Set();
    this.socket = null;
    this.backoff = 500;
    this.status = "connecting";
  }

  async get(path, { fresh = false } = {}) {
    if (!fresh && this.cache.has(path)) return this.cache.get(path);
    if (this.inflight.has(path)) return this.inflight.get(path);

    const promise = fetch(BASE + path, { headers: { accept: "application/json" } })
      .then(async (response) => {
        if (!response.ok) {
          const body = await response.json().catch(() => ({}));
          throw new ApiError(body.detail || response.statusText, response.status);
        }
        return response.json();
      })
      .then((data) => {
        this.cache.set(path, data);
        this.inflight.delete(path);
        return data;
      })
      .catch((error) => {
        this.inflight.delete(path);
        throw error;
      });

    this.inflight.set(path, promise);
    return promise;
  }

  async post(path, body) {
    const response = await fetch(BASE + path, {
      method: "POST",
      headers: { "content-type": "application/json", "x-ds-user": "analyst" },
      body: JSON.stringify(body || {}),
    });
    const data = await response.json().catch(() => ({}));
    if (!response.ok) throw new ApiError(data.detail || response.statusText, response.status);
    this.invalidate();
    return data;
  }

  invalidate(prefix = "") {
    for (const key of [...this.cache.keys()]) {
      if (key.startsWith(prefix)) this.cache.delete(key);
    }
  }

  // -- live ---------------------------------------------------------------

  connect() {
    const protocol = location.protocol === "https:" ? "wss" : "ws";
    let socket;
    try {
      socket = new WebSocket(protocol + "://" + location.host + "/ws/incidents");
    } catch {
      this.setStatus("down");
      return;
    }
    this.socket = socket;

    socket.addEventListener("open", () => {
      this.backoff = 500;
      this.setStatus("live");
      this.heartbeat = setInterval(() => {
        if (socket.readyState === WebSocket.OPEN) socket.send("ping");
      }, 25000);
    });

    socket.addEventListener("message", (event) => {
      let message;
      try {
        message = JSON.parse(event.data);
      } catch {
        return;
      }
      if (message.type === "pong" || message.type === "hello") return;
      // Anything the server tells us about invalidates only the slice it
      // touched, and listeners decide whether that slice is on screen.
      this.invalidate("/incidents");
      this.invalidate("/actions");
      this.emit(message);
    });

    socket.addEventListener("close", () => {
      clearInterval(this.heartbeat);
      this.setStatus("down");
      this.backoff = Math.min(this.backoff * 1.8, 15000);
      setTimeout(() => this.connect(), this.backoff);
    });

    socket.addEventListener("error", () => socket.close());
  }

  setStatus(status) {
    this.status = status;
    this.emit({ type: "connection", payload: { status } });
  }

  on(handler) {
    this.listeners.add(handler);
    return () => this.listeners.delete(handler);
  }

  emit(message) {
    for (const handler of this.listeners) {
      try {
        handler(message);
      } catch (error) {
        console.error("listener failed", error);
      }
    }
  }
}

export class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.status = status;
  }
}

export const api = new Api();
