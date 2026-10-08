/**
 * 会话模块：会话列表加载、新建、删除、重命名、切换。
 */
import { getJSON, postJSON } from './api.js';
import { clear, el, fmtRelative, statusLabel, toastErr, toastOk } from './components.js';

export class SessionPanel {
  /**
   * @param {HTMLElement} listEl 会话列表容器
   * @param {{onSelect:Function, onBeforeSwitch?:Function}} handlers
   */
  constructor(listEl, handlers = {}) {
    this.listEl = listEl;
    this.onSelect = handlers.onSelect || (() => {});
    this.conversations = [];
    this.activeId = null;
  }

  async load() {
    const data = await getJSON('/api/chat/ls');
    this.conversations = data.conversations || [];
    this.render();
    return this.conversations;
  }

  render() {
    clear(this.listEl);
    if (!this.conversations.length) {
      this.listEl.append(el('div', { class: 'panel-empty', text: '还没有会话\n点击上方「新建」开始分析' }));
      return;
    }

    for (const conv of this.conversations) {
      const item = el('div', {
        class: `conv-item${conv.conversation_id === this.activeId ? ' active' : ''}`,
        onclick: () => this.select(conv.conversation_id),
      }, [
        el('span', { class: 'ct', text: conv.title, title: conv.title }),
        el('div', { class: 'cm' }, [
          statusLabel(conv.status),
          ' · ',
          conv.last_message_at ? fmtRelative(conv.last_message_at) : '暂无消息',
          conv.attachment_count ? ` · ${conv.attachment_count} 附件` : '',
        ]),
        el('div', { class: 'cact' }, [
          el('button', {
            class: 'icon-btn', title: '重命名', text: '✎',
            onclick: (event) => { event.stopPropagation(); this.rename(conv); },
          }),
          el('button', {
            class: 'icon-btn', title: '删除', text: '🗑',
            onclick: (event) => { event.stopPropagation(); this.remove(conv); },
          }),
        ]),
      ]);
      this.listEl.append(item);
    }
  }

  async create(title = '') {
    const created = await postJSON('/api/chat/create', { title });
    await this.load();
    this.select(created.conversation_id);
    return created;
  }

  select(conversationId) {
    if (conversationId === this.activeId) return;
    this.activeId = conversationId;
    this.render();
    const conv = this.conversations.find((c) => c.conversation_id === conversationId);
    this.onSelect(conversationId, conv);
  }

  async rename(conv) {
    const title = window.prompt('重命名会话', conv.title);
    if (title === null) return;
    const trimmed = title.trim();
    if (!trimmed) { toastErr('标题不能为空'); return; }
    try {
      await postJSON('/api/chat/update', { conversation_id: conv.conversation_id, title: trimmed });
      await this.load();
      toastOk('已重命名');
    } catch (err) {
      toastErr(err.detail || '重命名失败');
    }
  }

  async remove(conv) {
    const ok = window.confirm(
      `确定删除会话「${conv.title}」？\n\n该会话下的消息、附件、分析任务、结果记录与文件目录都会被一并删除，且不可恢复。`
    );
    if (!ok) return;
    try {
      const result = await postJSON('/api/chat/delete', { conversation_ids: [conv.conversation_id] });
      if (!result.deleted_count) {
        toastErr(result.skipped?.[0]?.reason || '删除失败');
        return;
      }
      if (this.activeId === conv.conversation_id) {
        this.activeId = null;
        this.onSelect(null, null);
      }
      await this.load();
      toastOk('会话已删除');
    } catch (err) {
      toastErr(err.detail || '删除失败');
    }
  }

  setActive(conversationId) {
    this.activeId = conversationId;
    this.render();
  }

  current() {
    return this.conversations.find((c) => c.conversation_id === this.activeId) || null;
  }
}
