/**
 * 日志模块：实时任务区（当前轮状态、步骤条、工具执行过程、结果文件状态、错误信息）
 * 与任务日志查看。
 */
import { getJSON } from './api.js';
import { clear, el, statusLabel, statusTag, toastErr } from './components.js';

const STEPS = ['上下文准备', '执行分析', '生成结果', '完成'];

export class TaskPanel {
  /**
   * @param {HTMLElement} containerEl 实时任务区容器
   * @param {{onCancel?:Function, onRefreshResult?:Function}} handlers
   */
  constructor(containerEl, handlers = {}) {
    this.containerEl = containerEl;
    this.onCancel = handlers.onCancel || (() => {});
    this.onRefreshResult = handlers.onRefreshResult || (() => {});

    this.task = null;         // {task_id, task_status, current_step, ...}
    this.tools = [];          // [{name, summary}]
    this.resultReady = false;
    this.connection = 'closed';
  }

  reset() {
    this.task = null;
    this.tools = [];
    this.resultReady = false;
    this.render();
  }

  setConnection(state, detail) {
    this.connection = state;
    this.connectionDetail = detail;
    this.render();
  }

  setTask(task) {
    this.task = task;
    this.render();
  }

  applyEvent(event) {
    const type = event.type;
    if (type === 'task_status' || type === 'user_message_accepted') {
      this.task = this.task && this.task.task_id === event.task_id
        ? { ...this.task, ...event }
        : { task_id: event.task_id, task_status: event.task_status || 'queued', current_step: event.current_step };
      if (type === 'user_message_accepted') this.tools = [];
      this.resultReady = false;
      this.render();
      return true;
    }
    if (type === 'tool_start') {
      this.tools.push({ name: event.tool_name, summary: '执行中…', running: true });
      this.render();
      return true;
    }
    if (type === 'tool_finish') {
      const entry = [...this.tools].reverse().find((t) => t.name === event.tool_name && t.running);
      if (entry) {
        entry.running = false;
        entry.summary = event.tool_result_summary || '';
      } else {
        this.tools.push({ name: event.tool_name, summary: event.tool_result_summary || '' });
      }
      this.render();
      return true;
    }
    if (type === 'result_ready') {
      this.resultReady = true;
      this.render();
      return true;
    }
    if (type === 'error') {
      if (this.task) this.task.error_message = event.error_message;
      this.render();
      return true;
    }
    if (type === 'done') {
      if (this.task) {
        this.task.task_status = event.task_status || this.task.task_status;
        this.task.finished_at = event.finished_at;
      }
      this.render();
      return true;
    }
    return false;
  }

  /** 断线重连或切换会话后按任务 ID 拉取最新状态 */
  async syncFromServer(taskId) {
    try {
      const task = await getJSON(`/api/tasks/${taskId}`);
      this.task = task;
      this.render();
    } catch (_) { /* 任务可能已被删除 */ }
  }

  async loadLogs(taskId) {
    try {
      const data = await getJSON(`/api/tasks/${taskId}/logs`);
      this.logs = data.logs || [];
    } catch (_) {
      this.logs = [];
    }
    this.render();
  }

  render() {
    clear(this.containerEl);

    const connText = {
      open: '长连接已建立',
      connecting: '长连接建立中…',
      reconnecting: `长连接断开，第 ${this.connectionDetail || 1} 次重连中…`,
      closed: '长连接已断开',
      error: '长连接异常',
      giveup: '长连接不可用，请刷新页面重试',
    }[this.connection] || '未连接';

    this.containerEl.append(el('div', {
      class: `log-line ${this.connection === 'open' ? '' : 'lv-warning'}`,
      style: 'margin-bottom:10px;',
    }, [
      el('span', { class: 'lt', text: connText }),
    ]));

    if (!this.task) {
      this.containerEl.append(el('div', {
        class: 'panel-empty',
        text: '发送问题后\n这里会显示分析进度与工具执行过程',
      }));
      this._renderLogs();
      return;
    }

    const status = this.task.task_status;
    const currentStep = this.task.current_step || '';

    this.containerEl.append(el('div', { class: 'task-current' }, [
      el('div', { class: 'tch' }, [
        el('span', { text: `任务 #${this.task.task_id}` }),
        statusTag(status),
      ]),
      this._steps(currentStep, status),
      ['queued', 'running'].includes(status)
        ? el('button', {
            class: 'btn btn-sm btn-danger',
            style: 'width:100%;margin-top:10px;',
            text: '取消本轮分析',
            onclick: () => this.onCancel(this.task.task_id),
          })
        : null,
    ]));

    if (this.task.error_message) {
      this.containerEl.append(el('div', { class: 'log-line lv-error' }, [
        el('span', { class: 'lt', text: '错误' }),
        el('span', { class: 'lc', text: this.task.error_message }),
      ]));
    }

    if (this.tools.length) {
      this.containerEl.append(el('div', { class: 'cfg-group-title', text: `工具执行（${this.tools.length}）` }));
      const list = el('div', { class: 'log-list' });
      for (const tool of this.tools) {
        list.append(el('div', { class: `log-line${tool.running ? ' lv-warning' : ''}` }, [
          el('span', { class: 'lt', text: tool.running ? '…' : '✔' }),
          el('span', { class: 'lc' }, [
            el('code', { style: 'color:var(--primary);font-size:11.5px;', text: tool.name }),
            ' ',
            tool.summary || '',
          ]),
        ]));
      }
      this.containerEl.append(list);
    }

    this.containerEl.append(el('div', { class: 'cfg-group-title', text: '结果文件' }));
    this.containerEl.append(el('div', { class: `log-line${this.resultReady ? '' : ' lv-warning'}` }, [
      el('span', { class: 'lt', text: this.resultReady ? '✔' : '…' }),
      el('span', { class: 'lc', text: this.resultReady ? '结构化结果与导出文件已生成' : '尚未生成' }),
    ]));

    this._renderLogs();
  }

  _steps(currentStep, status) {
    const order = STEPS;
    let activeIndex = order.findIndex((s) => s === currentStep);
    if (currentStep === '排队中') activeIndex = -1;
    if (['success', 'failed', 'cancelled'].includes(status)) activeIndex = order.length;

    const wrap = el('div', { class: 'steps' });
    order.forEach((label, index) => {
      const done = index < activeIndex || status === 'success';
      const active = index === activeIndex && status === 'running';
      wrap.append(el('div', { class: `step${done ? ' done' : ''}${active ? ' active' : ''}` }, [
        el('span', { class: 'dot', text: done ? '✓' : '' }),
        el('span', { text: label }),
      ]));
    });
    if (status === 'cancelled') {
      wrap.append(el('div', { class: 'step done' }, [
        el('span', { class: 'dot', text: '×' }), el('span', { text: '已取消' }),
      ]));
    }
    if (status === 'failed') {
      wrap.append(el('div', { class: 'step done' }, [
        el('span', { class: 'dot', text: '!' }), el('span', { text: '已失败' }),
      ]));
    }
    return wrap;
  }

  _renderLogs() {
    if (!this.logs || !this.logs.length) return;
    this.containerEl.append(el('div', { class: 'cfg-group-title', text: `执行日志（${this.logs.length}）` }));
    const list = el('div', { class: 'log-list' });
    for (const entry of this.logs.slice(-40)) {
      list.append(el('div', { class: `log-line lv-${entry.log_level}` }, [
        el('span', { class: 'lt', text: entry.log_type }),
        el('span', { class: 'lc', text: entry.log_content }),
      ]));
    }
    this.containerEl.append(list);
  }
}
