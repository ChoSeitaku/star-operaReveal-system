/**
 * 附件模块：上传（含进度）、列表、删除、下载。
 *
 * 解析状态由服务端后台任务更新，这里轮询到不再是 pending/parsing 为止。
 */
import { ApiError, getJSON, postJSON, triggerDownload, uploadFile } from './api.js';
import { clear, el, fmtSize, fmtTime, statusTag, toastErr, toastOk } from './components.js';

const PARSE_POLL_INTERVAL = 800;
const PARSE_POLL_MAX = 15;

export class AttachmentPanel {
  /**
   * @param {HTMLElement} listEl 列表容器
   * @param {HTMLElement} zoneEl 拖拽上传区
   * @param {HTMLElement} inputEl 隐藏的 file input
   * @param {{onChange?:Function}} handlers
   */
  constructor(listEl, zoneEl, inputEl, handlers = {}) {
    this.listEl = listEl;
    this.zoneEl = zoneEl;
    this.inputEl = inputEl;
    this.onChange = handlers.onChange || (() => {});
    this.conversationId = null;
    this.items = [];
    this.uploading = new Map(); // 文件名 -> 进度
    this.pendingIds = new Set(); // 本次上传、待其解析完成的附件
  }

  setConversation(conversationId) {
    this.conversationId = conversationId;
    this.items = [];
    this.uploading.clear();
    this.pendingIds.clear();
    this.render();
    if (conversationId) this.refresh();
  }

  bindEvents() {
    this.zoneEl.addEventListener('click', () => this.inputEl.click());
    this.inputEl.addEventListener('change', () => {
      this.handleFiles(Array.from(this.inputEl.files || []));
      this.inputEl.value = '';
    });
    for (const type of ['dragenter', 'dragover']) {
      this.zoneEl.addEventListener(type, (event) => {
        event.preventDefault();
        this.zoneEl.classList.add('drag');
      });
    }
    for (const type of ['dragleave', 'drop']) {
      this.zoneEl.addEventListener(type, (event) => {
        event.preventDefault();
        this.zoneEl.classList.remove('drag');
      });
    }
    this.zoneEl.addEventListener('drop', (event) => {
      const files = Array.from(event.dataTransfer?.files || []);
      this.handleFiles(files);
    });
  }

  async refresh() {
    if (!this.conversationId) return;
    try {
      const data = await getJSON(`/api/attachment/ls?conversation_id=${this.conversationId}`);
      this.items = data.attachments || [];
      this.render();
    } catch (err) {
      toastErr(err.detail || '附件列表加载失败');
    }
  }

  async handleFiles(files) {
    if (!this.conversationId) { toastErr('请先选择或新建一个会话'); return; }
    if (!files.length) return;

    for (const file of files) {
      this.uploading.set(file.name, 0);
      this.render();
      try {
        const result = await uploadFile(this.conversationId, file, (ratio) => {
          this.uploading.set(file.name, ratio);
          this.render();
        });
        this.uploading.delete(file.name);
        this.pendingIds.add(result.attachment_id);
        toastOk(`已上传 ${result.file_name}（${fmtSize(result.file_size)}）`);
      } catch (err) {
        this.uploading.delete(file.name);
        const detail = err instanceof ApiError ? err.detail : (err.message || '上传失败');
        toastErr(`${file.name}：${detail}`);
      }
      this.render();
    }

    await this.refresh();
    this.pollParsing();
    this.onChange();
  }

  /** 轮询解析状态，直到本次上传的附件都不再是 pending/parsing */
  async pollParsing() {
    for (let round = 0; round < PARSE_POLL_MAX; round += 1) {
      if (!this.pendingIds.size) return;
      await new Promise((resolve) => setTimeout(resolve, PARSE_POLL_INTERVAL));
      await this.refresh();
      for (const item of this.items) {
        if (item.parse_status === 'success' || item.parse_status === 'failed') {
          this.pendingIds.delete(item.attachment_id);
        }
      }
      if (!this.pendingIds.size) { this.onChange(); return; }
    }
    this.pendingIds.clear();
  }

  async remove(attachmentId) {
    try {
      await postJSON('/api/attachment/delete', { attachment_id: attachmentId });
      toastOk('附件已删除');
      await this.refresh();
      this.onChange();
    } catch (err) {
      toastErr(err.detail || '删除失败');
    }
  }

  download(attachmentId) {
    triggerDownload(`/api/attachment/get?attachment_id=${attachmentId}`);
  }

  render() {
    clear(this.listEl);

    if (!this.conversationId) {
      this.listEl.append(el('div', { class: 'panel-empty', text: '请先选择或新建一个会话' }));
      return;
    }

    this.zoneEl.style.display = '';

    for (const [name, ratio] of this.uploading) {
      this.listEl.append(el('div', { class: 'attach-item' }, [
        el('div', { class: 'an', text: name }),
        el('div', { class: 'am', text: `上传中 ${Math.round(ratio * 100)}%` }),
        el('div', { class: 'progress' }, [el('i', { style: `width:${ratio * 100}%` })]),
      ]));
    }

    if (!this.items.length && !this.uploading.size) {
      this.listEl.append(el('div', {
        class: 'panel-empty',
        text: '暂无附件\n支持 CSV / TXT / MD / JSON / LOG / XLSX',
      }));
      return;
    }

    for (const item of this.items) {
      this.listEl.append(el('div', { class: 'attach-item' }, [
        el('div', { class: 'an', text: item.file_name, title: item.file_name }),
        el('div', { class: 'am' }, [
          statusTag(item.parse_status),
          el('span', { class: 'tag tag-mute', text: (item.file_type || 'unknown').toUpperCase() }),
          el('span', { text: fmtSize(item.file_size) }),
          el('span', { text: fmtTime(item.created_at) }),
        ]),
        el('div', { class: 'aa' }, [
          el('button', {
            class: 'btn btn-sm', text: '下载',
            onclick: () => this.download(item.attachment_id),
          }),
          el('button', {
            class: 'btn btn-sm btn-danger', text: '删除',
            onclick: () => this.remove(item.attachment_id),
          }),
        ]),
      ]));
    }
  }

  /** 已解析成功的附件 ID，随消息一起发送 */
  parsedIds() {
    return this.items.filter((i) => i.parse_status === 'success').map((i) => i.attachment_id);
  }
}
