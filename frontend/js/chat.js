/**
 * 聊天模块：消息发送、流式渲染、取消分析、历史消息回放。
 *
 * 实时事件按 task_id 归属到消息气泡：message_delta 追加到当前助手气泡，
 * tool_start / tool_finish 渲染为工具卡片，done 则收尾并触发结果区刷新。
 */
import { clear, el, fmtTime } from './components.js';

export class ChatPanel {
  /**
   * @param {HTMLElement} scrollEl 消息滚动容器
   * @param {{onTaskDone?:Function, onTaskStart?:Function}} handlers
   */
  constructor(scrollEl, handlers = {}) {
    this.scrollEl = scrollEl;
    this.onTaskDone = handlers.onTaskDone || (() => {});
    this.onTaskStart = handlers.onTaskStart || (() => {});

    this.currentTaskId = null;
    this.currentBubble = null;   // 当前流式助手气泡
    this.currentText = '';
    this.toolCards = new Map();  // taskId -> [toolCard]
  }

  reset() {
    clear(this.scrollEl);
    this.currentTaskId = null;
    this.currentBubble = null;
    this.currentText = '';
    this.toolCards.clear();
  }

  _scrollToBottom(force = false) {
    const el0 = this.scrollEl;
    const nearBottom = el0.scrollHeight - el0.scrollTop - el0.clientHeight < 160;
    if (force || nearBottom) el0.scrollTop = el0.scrollHeight;
  }

  showEmpty() {
    this.reset();
    this.scrollEl.append(el('div', { class: 'panel-empty' }, [
      el('div', { text: '开始一次归因分析' }),
      el('div', {
        style: 'margin-top:10px;font-size:12px;',
        text: '示例：近30天各品类的搜索曝光到转化漏斗表现如何？',
      }),
      el('div', {
        style: 'margin-top:4px;font-size:12px;',
        text: '示例：各渠道和用户分层在访问→加购→下单漏斗上的转化表现如何？',
      }),
    ]));
  }

  /* ------------------------------------------------------------ 渲染 */

  appendUserMessage(content, createdAt, attachments = []) {
    const bubble = el('div', { class: 'bubble', text: content });
    const wrap = el('div', { class: 'msg msg-user' }, [bubble]);
    if (attachments.length) {
      wrap.append(el('div', { class: 'msg-meta', text: `附件：${attachments.map((a) => a.file_name).join('、')}` }));
    }
    wrap.append(el('div', { class: 'msg-meta', text: fmtTime(createdAt) }));
    this.scrollEl.append(wrap);
    this._scrollToBottom(true);
    return wrap;
  }

  appendAssistantMessage(content, createdAt, { streaming = false } = {}) {
    const bubble = el('div', { class: `bubble${streaming ? ' streaming' : ''}`, text: content });
    const wrap = el('div', { class: 'msg msg-assistant' }, [
      bubble,
      el('div', { class: 'msg-meta', text: createdAt ? fmtTime(createdAt) : '' }),
    ]);
    this.scrollEl.append(wrap);
    if (streaming) {
      this.currentBubble = bubble;
      this.currentText = content;
    }
    this._scrollToBottom(true);
    return bubble;
  }

  appendToolMessage(toolName, content, status, createdAt) {
    const card = el('div', { class: 'msg-tool' }, [
      el('div', { class: 'th' }, [
        el('span', { text: status === 'error' ? '✘' : '✔' }),
        el('code', { text: toolName || 'tool' }),
        el('span', { style: 'margin-left:auto;font-size:11px;', text: fmtTime(createdAt) }),
      ]),
      el('div', { class: 'tb', text: String(content || '').slice(0, 400) }),
    ]);
    this.scrollEl.append(card);
    this._scrollToBottom();
    return card;
  }

  /** 回放整段历史 */
  replay(messages) {
    this.reset();
    if (!messages.length) { this.showEmpty(); return; }
    for (const message of messages) {
      if (message.role === 'user') {
        this.appendUserMessage(message.content, message.created_at, message.attachments || []);
      } else if (message.role === 'assistant') {
        this.appendAssistantMessage(message.content, message.created_at);
      } else if (message.role === 'tool') {
        this.appendToolMessage(message.tool_name, message.content, message.tool_status, message.created_at);
      }
    }
    // 正在运行的任务：补一个流式气泡承接后续增量
    const running = messages.filter((m) => m.task && ['queued', 'running'].includes(m.task.task_status)).pop();
    if (running) {
      this.beginStream(running.task.task_id);
    }
    this._scrollToBottom(true);
  }

  /* ------------------------------------------------------------ 实时事件 */

  beginStream(taskId) {
    if (this.currentTaskId === taskId && this.currentBubble) return;
    this.currentTaskId = taskId;
    this.currentText = '';
    this.currentBubble = null;
    this.toolCards.set(taskId, []);
    this.onTaskStart(taskId);
  }

  /** 处理一条服务端事件；返回是否为任务级事件 */
  handleEvent(event) {
    const type = event.type;
    if (type === 'user_message_accepted') {
      this.beginStream(event.task_id);
      return true;
    }
    if (!this.currentTaskId && event.task_id) this.beginStream(event.task_id);
    if (event.task_id && this.currentTaskId && event.task_id !== this.currentTaskId) return false;

    switch (type) {
      case 'message_start':
        this.beginStream(event.task_id);
        return true;

      case 'message_delta':
        this._appendDelta(event.delta_text || '');
        return true;

      case 'tool_start':
        this._addToolCard(event.task_id, event.tool_name);
        return true;

      case 'tool_finish':
        this._finishToolCard(event.task_id, event.tool_name, event.tool_result_summary);
        return true;

      case 'result_ready':
        this.onTaskDone({ kind: 'result', taskId: event.task_id, resultId: event.result_id });
        return true;

      case 'error':
        this._appendDelta(`\n\n⚠ ${event.error_message || '分析出错'}`);
        this._finishStream();
        return true;

      case 'done':
        this._finishStream();
        this.onTaskDone({ kind: 'done', taskId: event.task_id, status: event.task_status });
        return true;

      default:
        return false;
    }
  }

  _appendDelta(delta) {
    if (!delta) return;
    if (!this.currentBubble) {
      const bubble = el('div', { class: 'bubble streaming', text: '' });
      this.scrollEl.append(el('div', { class: 'msg msg-assistant' }, [bubble]));
      this.currentBubble = bubble;
      this.currentText = '';
    }
    this.currentText += delta;
    this.currentBubble.textContent = this.currentText;
    this._scrollToBottom();
  }

  _addToolCard(taskId, toolName) {
    const card = el('div', { class: 'msg-tool' }, [
      el('div', { class: 'th' }, [
        el('span', { class: 'spinner', style: 'width:12px;height:12px;border-width:2px;margin:0;' }),
        el('code', { text: toolName || 'tool' }),
        el('span', { style: 'margin-left:auto;font-size:11px;', text: '执行中…' }),
      ]),
    ]);
    const list = this.toolCards.get(taskId) || [];
    list.push({ toolName, card });
    this.toolCards.set(taskId, list);
    this.scrollEl.append(card);
    this._scrollToBottom();
  }

  _finishToolCard(taskId, toolName, summary) {
    const list = this.toolCards.get(taskId) || [];
    const entry = [...list].reverse().find((t) => t.toolName === toolName && !t.done);
    if (!entry) return;
    entry.done = true;
    clear(entry.card);
    entry.card.append(
      el('div', { class: 'th' }, [
        el('span', { text: '✔' }),
        el('code', { text: toolName }),
        el('span', { style: 'margin-left:auto;font-size:11px;', text: '完成' }),
      ]),
      el('div', { class: 'tb', text: String(summary || '').slice(0, 400) }),
    );
    this._scrollToBottom();
  }

  _finishStream() {
    if (this.currentBubble) {
      this.currentBubble.classList.remove('streaming');
      if (!this.currentText.trim()) {
        this.currentBubble.textContent = '（本轮分析无文本输出，请查看右侧结构化结果）';
      }
    }
    this.currentTaskId = null;
    this.currentBubble = null;
    this.currentText = '';
  }

  isStreaming() {
    return !!this.currentTaskId;
  }
}
