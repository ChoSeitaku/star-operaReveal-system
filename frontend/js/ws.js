/**
 * 长连接客户端：令牌签发 → 连接 → 事件分发 → 自动重连 → 轮询兜底。
 *
 * 服务端令牌是一次性且短时效的，因此每次（重）连都必须重新签发。
 * 断线期间错过的增量文本无法补发，重连后会通过服务端下发的任务快照
 * 与会话历史回放补齐最终状态。
 */
import { getJSON, postJSON } from './api.js';

const PING_INTERVAL = 25000;
const MAX_RECONNECT = 4;

export class ChatSocket {
  /**
   * @param {number} conversationId
   * @param {{onEvent?:Function, onState?:Function}} handlers
   */
  constructor(conversationId, handlers = {}) {
    this.conversationId = conversationId;
    this.onEvent = handlers.onEvent || (() => {});
    this.onState = handlers.onState || (() => {});

    this.socket = null;
    this.closedByUser = false;
    this.attempts = 0;
    this.pingTimer = null;
    this.reconnectTimer = null;
    this.currentTaskId = null;
  }

  async connect() {
    this.closedByUser = false;
    this.onState('connecting');
    try {
      const token = await postJSON('/api/chat/ws-token', { conversation_id: this.conversationId });
      const proto = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
      const url = `${proto}//${window.location.host}/api/chat/ws/chat` +
                  `?websocket_token=${encodeURIComponent(token.websocket_token)}` +
                  `&conversation_id=${this.conversationId}`;
      this.socket = new WebSocket(url);
    } catch (err) {
      this.onState('error', err.detail || err.message);
      this._scheduleReconnect();
      return;
    }

    this.socket.addEventListener('open', () => {
      this.attempts = 0;
      this.onState('open');
      this._startPing();
    });

    this.socket.addEventListener('message', (event) => {
      let payload;
      try { payload = JSON.parse(event.data); } catch (_) { return; }
      if (payload.type === 'pong') return;
      if (payload.task_id) this.currentTaskId = payload.task_id;
      this.onEvent(payload);
    });

    this.socket.addEventListener('close', (event) => {
      this._stopPing();
      this.onState('closed', event.code, event.reason);
      if (!this.closedByUser) this._scheduleReconnect();
    });

    this.socket.addEventListener('error', () => {
      this.onState('error', '连接出错');
    });
  }

  _startPing() {
    this._stopPing();
    this.pingTimer = setInterval(() => this.send({ type: 'ping' }), PING_INTERVAL);
  }

  _stopPing() {
    if (this.pingTimer) clearInterval(this.pingTimer);
    this.pingTimer = null;
  }

  _scheduleReconnect() {
    if (this.closedByUser || this.reconnectTimer) return;
    if (this.attempts >= MAX_RECONNECT) {
      this.onState('giveup');
      return;
    }
    this.attempts += 1;
    const delay = Math.min(1000 * 2 ** (this.attempts - 1), 8000);
    this.onState('reconnecting', this.attempts, delay);
    this.reconnectTimer = setTimeout(() => {
      this.reconnectTimer = null;
      this.connect();
    }, delay);
  }

  /** 轮询兜底：长连接不可用时用 REST 查询任务状态 */
  async pollTask(taskId) {
    try {
      return await getJSON(`/api/tasks/${taskId}`);
    } catch (_) {
      return null;
    }
  }

  send(payload) {
    if (this.socket && this.socket.readyState === WebSocket.OPEN) {
      this.socket.send(JSON.stringify(payload));
      return true;
    }
    return false;
  }

  sendMessage(content, attachmentIds = []) {
    return this.send({ type: 'user_message', content, attachment_ids: attachmentIds });
  }

  cancelTask(taskId) {
    return this.send({ type: 'cancel', task_id: taskId });
  }

  isOpen() {
    return !!this.socket && this.socket.readyState === WebSocket.OPEN;
  }

  close() {
    this.closedByUser = true;
    this._stopPing();
    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }
    if (this.socket) {
      try { this.socket.close(); } catch (_) { /* ignore */ }
      this.socket = null;
    }
  }
}

/** 管理台用的系统状态轮询器 */
export function startStatsPolling(intervalMs, onData) {
  let timer = null;
  const tick = async () => {
    try { onData(await getJSON('/api/admin/stats')); } catch (_) { /* ignore */ }
  };
  tick();
  timer = setInterval(tick, intervalMs);
  return () => clearInterval(timer);
}
