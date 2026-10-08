/**
 * 结果模块：六段式结构化结果展示、复制、导出。
 *
 * 六段：问题定义 / 关键指标 / 证据列表 / 归因结论 / 待补充数据 / 下一步建议。
 */
import { getJSON, triggerDownload } from './api.js';
import {
  clear, confidenceTag, copyText, el, metricText, toastErr, toastOk,
} from './components.js';

export class ResultPanel {
  /** @param {HTMLElement} containerEl 结果区容器 */
  constructor(containerEl) {
    this.containerEl = containerEl;
    this.data = null;
  }

  reset() {
    this.data = null;
    clear(this.containerEl);
    this.containerEl.append(el('div', {
      class: 'panel-empty',
      text: '本轮分析完成后\n结构化结论会显示在这里',
    }));
  }

  async load(taskId) {
    try {
      this.data = await getJSON(`/api/results/${taskId}`);
      this.render();
      return this.data;
    } catch (err) {
      if (err.status === 404) {
        // 任务尚未产出结果，保持占位即可
        return null;
      }
      toastErr(err.detail || '结果加载失败');
      return null;
    }
  }

  render() {
    clear(this.containerEl);
    if (!this.data) { this.reset(); return; }
    const data = this.data;

    this.containerEl.append(el('div', { class: 'result-head' }, [
      el('h3', { text: '本轮分析结果' }),
      el('div', { class: 'result-actions' }, [
        el('button', { class: 'btn btn-sm', text: '复制', onclick: () => this.copy() }),
        el('button', {
          class: 'btn btn-sm', text: '复制 Markdown',
          onclick: () => this.copyMarkdown(),
        }),
        el('button', {
          class: 'btn btn-sm btn-primary', text: '导出文件',
          onclick: () => this.export(),
        }),
      ]),
    ]));

    this.containerEl.append(this._section('① 问题定义', [
      el('div', { text: data.problem_definition || '-' }),
    ]));

    this.containerEl.append(this._section(
      '② 关键指标',
      [this._metricsTable(data.key_metrics || [])],
      `${(data.key_metrics || []).length} 项`,
    ));

    this.containerEl.append(this._section(
      '③ 证据列表',
      this._evidenceList(data.evidence_list || []),
      `${(data.evidence_list || []).length} 条`,
    ));

    this.containerEl.append(this._section(
      '④ 归因结论',
      [el('div', { style: 'white-space:pre-wrap;', text: data.conclusion_text || '-' })],
      '',
      'conclusion',
    ));

    this.containerEl.append(this._section('⑤ 待补充数据', [
      el('div', { style: 'white-space:pre-wrap;', text: data.missing_data_text || '-' }),
    ]));

    this.containerEl.append(this._section('⑥ 下一步建议', this._actionList(data.next_action_text)));

    if (data.result_file_path) {
      this.containerEl.append(el('div', {
        style: 'font-size:11.5px;color:var(--text-mute);margin-top:6px;',
        text: `结果文件：${data.result_file_path}`,
      }));
    }
  }

  _section(title, bodyNodes, badge = '', extraClass = '') {
    const head = el('div', { class: 'sh' }, [el('span', { text: title })]);
    if (badge) head.append(el('span', { class: 'n', text: badge }));
    return el('div', { class: `sec ${extraClass}` }, [head, el('div', { class: 'sb' }, bodyNodes)]);
  }

  _metricsTable(metrics) {
    if (!metrics.length) return el('div', { class: 'panel-empty', text: '未产出关键指标' });
    return el('table', { class: 'metrics' }, [
      el('thead', {}, [el('tr', {}, [
        el('th', { text: '指标' }), el('th', { text: '数值' }),
        el('th', { text: '单位' }), el('th', { text: '周期' }),
      ])]),
      el('tbody', {}, metrics.map((m) => el('tr', {}, [
        el('td', { text: m.metric_name }),
        el('td', { class: 'num', text: metricText(m) }),
        el('td', { text: m.metric_unit || '-' }),
        el('td', { text: m.metric_period || '-' }),
      ]))),
    ]);
  }

  _evidenceList(evidence) {
    if (!evidence.length) return el('div', { class: 'panel-empty', text: '未产出证据' });
    return evidence.map((item) => el('div', { class: 'evidence' }, [
      el('div', { class: 'eh' }, [
        el('span', { class: 'tag tag-primary', text: item.source_type || '来源' }),
        el('span', { class: 'es', text: item.source_name || '' }),
        confidenceTag(item.confidence),
        item.related_metric ? el('span', { class: 'es', text: `关联：${item.related_metric}` }) : null,
      ]),
      el('div', { class: 'et', text: item.evidence_text || '' }),
    ]));
  }

  _actionList(text) {
    const items = String(text || '')
      .split('\n')
      .map((line) => line.replace(/^\s*(?:\d+\s*[.、)）]|[-•*])\s*/, '').trim())
      .filter(Boolean);
    if (!items.length) return [el('div', { class: 'panel-empty', text: '未产出建议' })];
    return [el('ol', { class: 'bullets' }, items.map((item) => el('li', { text: item })))];
  }

  /* ------------------------------------------------------------ 复制 / 导出 */

  _plainText() {
    const d = this.data;
    if (!d) return '';
    const lines = [
      `【问题定义】\n${d.problem_definition}`,
      `【关键指标】`,
      ...(d.key_metrics || []).map(
        (m) => `  - ${m.metric_name}: ${metricText(m)}（${m.metric_period || '-'}）`,
      ),
      `【证据列表】`,
      ...(d.evidence_list || []).map(
        (e) => `  - [${e.source_type}] ${e.source_name}（置信度 ${e.confidence}）：${e.evidence_text}`,
      ),
      `【归因结论】\n${d.conclusion_text}`,
      `【待补充数据】\n${d.missing_data_text}`,
      `【下一步建议】\n${d.next_action_text}`,
    ];
    return lines.join('\n');
  }

  async copy() {
    if (!this.data) { toastErr('暂无可复制的结果'); return; }
    (await copyText(this._plainText())) ? toastOk('结果已复制到剪贴板') : toastErr('复制失败，请手动选择文本');
  }

  async copyMarkdown() {
    if (!this.data) { toastErr('暂无可复制的结果'); return; }
    const text = this.data.result_markdown || this._plainText();
    (await copyText(text)) ? toastOk('Markdown 已复制') : toastErr('复制失败');
  }

  export() {
    if (!this.data) { toastErr('暂无可导出的结果'); return; }
    if (!this.data.has_file) { toastErr('导出文件不可用，请重新运行分析'); return; }
    triggerDownload(`/api/results/${this.data.task_id}/download`);
    toastOk('已开始下载结果文件');
  }
}
