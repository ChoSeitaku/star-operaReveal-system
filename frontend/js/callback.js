/**
 * 登录回调页逻辑：接收回调结果 → 校验登录态 → 自动跳转聊天工作台。
 */
import { parseCallbackParams, roleLabel, sessionState, startLogin } from './auth.js';
import { el } from './components.js';

const STATUS_NODES = {
  wait: 'state-wait',
  ok: 'state-ok',
  err: 'state-err',
};

function show(which) {
  for (const [key, id] of Object.entries(STATUS_NODES)) {
    document.getElementById(id).style.display = key === which ? '' : 'none';
  }
}

function goWorkspace() {
  window.location.href = '/app';
}

async function boot() {
  const params = parseCallbackParams();

  document.getElementById('goto-btn').addEventListener('click', goWorkspace);
  document.getElementById('retry-btn').addEventListener('click', () => startLogin());

  if (params.status === 'error') {
    document.getElementById('err-detail').textContent = params.message || '认证中心未返回有效授权。';
    show('err');
    return;
  }

  show('wait');
  const state = await sessionState();
  if (!state.authenticated) {
    document.getElementById('err-detail').textContent =
      params.message || '登录态校验失败，请重新登录。';
    show('err');
    return;
  }
  const user = state.user;

  const detail = document.getElementById('ok-detail');
  detail.textContent = `已登录：${user.display_name}（${roleLabel(user.role)}）`;
  if (params.displayName && params.displayName !== user.display_name) {
    detail.append(el('div', {
      style: 'font-size:12px;color:var(--text-mute);margin-top:4px;',
      text: `授权返回：${params.displayName}${params.role ? ` / ${params.role}` : ''}`,
    }));
  }
  show('ok');
  setTimeout(goWorkspace, 1200);
}

boot().catch((err) => {
  document.getElementById('err-detail').textContent = `回调处理失败：${err.message}`;
  show('err');
});
