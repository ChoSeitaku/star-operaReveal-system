/**
 * 配置模块：系统配置查看、修改、重载。
 *
 * 被进程环境变量强制覆盖的配置项标记为只读 —— 数据库中的值不会生效，
 * 必须调整环境变量后重启服务。
 */
import { getJSON, postJSON } from './api.js';
import { clear, el, fmtTime, toastErr, toastOk } from './components.js';

const GROUP_TITLES = {
  llm: '大模型（llm）',
  auth: '认证（auth）',
  analysis: '分析引擎（analysis）',
  upload: '附件上传（upload）',
};

export class ConfigPanel {
  /** @param {HTMLElement} containerEl @param {HTMLElement} statusEl */
  constructor(containerEl, statusEl) {
    this.containerEl = containerEl;
    this.statusEl = statusEl;
    this.items = [];
    this.edits = new Map(); // config_key -> 新值
    this.engineMode = '';
  }

  async load() {
    const data = await getJSON('/api/admin/config');
    this.items = data.items || [];
    this.engineMode = data.engine_mode || '';
    this.edits.clear();
    this.render();
  }

  render() {
    clear(this.containerEl);
    const groups = {};
    for (const item of this.items) {
      (groups[item.config_group] = groups[item.config_group] || []).push(item);
    }

    for (const [group, items] of Object.entries(groups)) {
      this.containerEl.append(el('div', { class: 'cfg-group-title', text: GROUP_TITLES[group] || group }));
      for (const item of items) {
        const locked = !!item.overridden_by_env;
        const input = el('input', {
          value: item.config_value,
          disabled: locked,
          oninput: (event) => this.edits.set(item.config_key, event.target.value),
        });
        this.containerEl.append(el('div', { class: 'cfg-row' }, [
          el('label', { title: item.config_key }, [
            item.config_key,
            locked ? el('span', { class: 'tag tag-warning', style: 'margin-left:6px;', text: '环境变量' }) : null,
          ]),
          input,
        ]));
      }
    }
  }

  async save() {
    if (!this.edits.size) { toastOk('没有需要保存的修改'); return; }
    const configs = Object.fromEntries(this.edits);
    try {
      const result = await postJSON('/api/admin/config', { configs });
      toastOk(result.message);
      if (result.overridden_by_env?.length) {
        toastErr(`以下配置被环境变量覆盖，本次修改不会生效：${result.overridden_by_env.join('、')}`);
      }
      await this.load();
    } catch (err) {
      toastErr(err.detail || '保存失败');
    }
  }

  async reload() {
    try {
      const result = await postJSON('/api/admin/reload');
      this.setStatus(result.message);
      toastOk(result.message);
      await this.load();
    } catch (err) {
      toastErr(err.detail || '重载失败');
    }
  }

  setStatus(text) {
    if (!this.statusEl) return;
    clear(this.statusEl);
    this.statusEl.append(el('span', { text: `${fmtTime(new Date().toISOString())}　${text}` }));
  }
}

/** 运行状态概览卡片 */
export function renderStats(container, stats) {
  clear(container);
  const rows = [
    ['分析引擎', stats.engine_mode],
    ['用户数', stats.users],
    ['会话总数', `${stats.conversations_total}（进行中 ${stats.conversations_active}）`],
    ['消息数', stats.messages],
    ['附件数', stats.attachments],
    ['分析任务', `${stats.tasks_total}（运行中 ${stats.tasks_running}，失败 ${stats.tasks_failed}）`],
    ['结构化结果', stats.results],
    ['上下文摘要', stats.summaries],
    ['长连接', `${stats.websocket.connections} 个连接 / ${stats.websocket.conversations} 个会话`],
    ['中断清理', `${stats.cleanup_interrupted} 个任务`],
    ['数据目录', stats.data_dir],
  ];
  const table = el('table', { class: 'metrics' }, [
    el('tbody', {}, rows.map(([k, v]) => el('tr', {}, [
      el('td', { text: k }),
      el('td', { class: 'num', text: String(v) }),
    ]))),
  ]);
  container.append(table);
}

/** 数据源连接表 */
export function renderDatasources(container, data) {
  clear(container);
  if (!data.datasources?.length) {
    container.append(el('div', { class: 'panel-empty', text: `未发现情景数据库\n目录：${data.scenario_db_dir}` }));
    return;
  }
  for (const ds of data.datasources) {
    const tables = el('details', {}, [
      el('summary', { style: 'cursor:pointer;font-size:12px;color:var(--text-dim);', text: `查看 ${ds.table_count} 张表` }),
      el('div', { style: 'margin-top:6px;font-size:11.5px;color:var(--text-mute);font-family:var(--mono);' },
        ds.tables.map((t) => el('div', { text: `${t.name}(${t.columns.join(', ')}) · ${t.row_count} 行` }))),
    ]);
    container.append(el('div', { class: 'attach-item' }, [
      el('div', { class: 'an', text: `${ds.name}　`, }, [
        el('span', { class: 'tag tag-primary', text: `${ds.table_count} 表` }),
      ]),
      el('div', { class: 'am' }, [
        el('span', { text: ds.file }),
        el('span', { text: `${ds.row_total.toLocaleString()} 行` }),
      ]),
      ds.error ? el('div', { class: 'am', style: 'color:var(--danger);', text: ds.error }) : null,
      tables,
    ]));
  }
}

/** 运行日志列表 */
export function renderLogs(container, logs) {
  clear(container);
  if (!logs.length) {
    container.append(el('div', { class: 'panel-empty', text: '暂无日志' }));
    return;
  }
  const list = el('div', { class: 'log-list' });
  for (const entry of logs) {
    list.append(el('div', { class: `log-line lv-${entry.log_level}` }, [
      el('span', { class: 'lt', text: `${entry.log_type}${entry.task_id ? `#${entry.task_id}` : ''}` }),
      el('span', { class: 'lc' }, [
        el('span', { style: 'color:var(--text-mute);font-size:10.8px;', text: `${fmtTime(entry.created_at)}　` }),
        entry.log_content,
      ]),
    ]));
  }
  container.append(list);
}
