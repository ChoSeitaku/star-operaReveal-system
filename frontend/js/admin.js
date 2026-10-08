/**
 * 管理台：配置查看/修改/热重载、运行日志、数据源连接、工具自检、运行状态。
 * 仅 role=admin 可访问。
 */
import { getJSON, postJSON } from './api.js';
import { currentUser, logout, roleLabel } from './auth.js';
import { ConfigPanel, renderDatasources, renderLogs, renderStats } from './config.js';
import { startStatsPolling } from './ws.js';
import { clear, el, toastErr, toastOk } from './components.js';

async function boot() {
  const user = await currentUser();
  if (!user) { window.location.href = '/auth/login'; return; }
  if (user.role !== 'admin') {
    document.getElementById('admin-body').innerHTML =
      '<div class="panel-empty">需要系统管理员权限。当前账号：' +
      `${user.display_name}（${roleLabel(user.role)}）</div>`;
    return;
  }

  document.getElementById('user-box').append(
    el('span', { class: 'who', text: `${user.display_name}（${roleLabel(user.role)}）` }),
    el('a', { class: 'btn btn-sm', href: '/app', text: '返回工作台' }),
    el('button', { class: 'btn btn-sm btn-ghost', text: '退出', onclick: () => logout() }),
  );

  const configPanel = new ConfigPanel(
    document.getElementById('config-list'),
    document.getElementById('config-status'),
  );

  document.getElementById('save-config').addEventListener('click', () => configPanel.save());
  document.getElementById('reload-config').addEventListener('click', () => configPanel.reload());
  document.getElementById('refresh-logs').addEventListener('click', () => loadLogs());
  document.getElementById('run-selftest').addEventListener('click', () => runSelftest());
  document.getElementById('refresh-ds').addEventListener('click', () => loadDatasources());

  async function loadLogs() {
    const level = document.getElementById('log-level').value;
    const query = level ? `?log_level=${level}&limit=300` : '?limit=300';
    try {
      const data = await getJSON(`/api/admin/logs${query}`);
      renderLogs(document.getElementById('log-list'), data.logs || []);
    } catch (err) {
      toastErr(err.detail || '日志加载失败');
    }
  }

  async function loadDatasources() {
    try {
      renderDatasources(document.getElementById('ds-list'), await getJSON('/api/admin/datasources'));
    } catch (err) {
      toastErr(err.detail || '数据源加载失败');
    }
  }

  async function runSelftest() {
    const host = document.getElementById('selftest-result');
    clear(host);
    host.append(el('div', { class: 'panel-empty', text: '正在执行工具自检…' }));
    try {
      const data = await postJSON('/api/admin/selftest');
      clear(host);
      host.append(el('div', {
        class: `log-line ${data.passed === data.total ? '' : 'lv-warning'}`,
        style: 'margin-bottom:8px;',
      }, [el('span', { class: 'lc', text: data.message })]));
      for (const item of data.results) {
        host.append(el('div', { class: `log-line ${item.ok ? '' : 'lv-error'}` }, [
          el('span', { class: 'lt', text: item.ok ? '✔' : '✘' }),
          el('span', { class: 'lc' }, [
            el('code', { style: 'color:var(--primary);', text: item.tool }),
            ' ',
            item.summary,
          ]),
        ]));
      }
      toastOk(data.message);
    } catch (err) {
      clear(host);
      toastErr(err.detail || '自检失败');
    }
  }

  // 运行状态轮询
  startStatsPolling(5000, (stats) => {
    renderStats(document.getElementById('stats'), stats);
    document.getElementById('stats-time').textContent = stats.server_time;
  });

  await configPanel.load();
  await loadLogs();
  await loadDatasources();
}

boot().catch((err) => {
  console.error(err);
  toastErr(`管理台初始化失败：${err.message}`);
});
