/**
 * 通用 UI 构件与格式化助手（无框架，纯 DOM）。
 */

/** 创建元素：el('div', {class:'x', onclick:fn}, [child, 'text']) */
export function el(tag, attrs = {}, children = []) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs)) {
    if (value === null || value === undefined || value === false) continue;
    if (key === 'class') node.className = value;
    else if (key === 'text') node.textContent = value;
    else if (key === 'html') node.innerHTML = value;
    else if (key === 'dataset') Object.assign(node.dataset, value);
    else if (key.startsWith('on') && typeof value === 'function') {
      node.addEventListener(key.slice(2).toLowerCase(), value);
    } else if (value === true) node.setAttribute(key, '');
    else node.setAttribute(key, value);
  }
  for (const child of [].concat(children)) {
    if (child === null || child === undefined || child === false) continue;
    node.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return node;
}

export function clear(node) {
  while (node.firstChild) node.removeChild(node.firstChild);
}

/** 后端统一返回 UTC ISO-8601，这里转成本地时间展示 */
export function fmtTime(iso) {
  if (!iso) return '-';
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso;
  const pad = (n) => String(n).padStart(2, '0');
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
         `${pad(date.getHours())}:${pad(date.getMinutes())}:${pad(date.getSeconds())}`;
}

export function fmtRelative(iso) {
  if (!iso) return '';
  const then = new Date(iso).getTime();
  if (Number.isNaN(then)) return '';
  const diff = Date.now() - then;
  if (diff < 60_000) return '刚刚';
  if (diff < 3_600_000) return `${Math.floor(diff / 60_000)} 分钟前`;
  if (diff < 86_400_000) return `${Math.floor(diff / 3_600_000)} 小时前`;
  if (diff < 7 * 86_400_000) return `${Math.floor(diff / 86_400_000)} 天前`;
  return fmtTime(iso).slice(0, 10);
}

export function fmtSize(bytes) {
  const n = Number(bytes) || 0;
  if (n < 1024) return `${n} B`;
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`;
  if (n < 1024 * 1024 * 1024) return `${(n / 1024 / 1024).toFixed(1)} MB`;
  return `${(n / 1024 / 1024 / 1024).toFixed(2)} GB`;
}

/** 指标数值展示：百分号等符号单位不留空格，且避免单位重复 */
export function metricText(metric) {
  const value = String(metric.metric_value ?? '');
  const unit = String(metric.metric_unit ?? '');
  if (!unit) return value;
  if (value.includes(unit)) return value;
  const tight = unit === '%' || unit === '‰' || unit === '°';
  return tight ? `${value}${unit}` : `${value} ${unit}`;
}

const STATUS_LABELS = {
  active: '进行中', archived: '已归档', deleted: '已删除',
  queued: '排队中', running: '执行中', success: '已完成',
  failed: '失败', cancelled: '已取消',
  pending: '待解析', parsing: '解析中',
};

const STATUS_CLASS = {
  active: 'tag-primary', running: 'tag-primary', parsing: 'tag-primary',
  queued: 'tag-mute', archived: 'tag-mute', pending: 'tag-mute',
  success: 'tag-success',
  failed: 'tag-danger', cancelled: 'tag-warning',
};

export function statusLabel(status) {
  return STATUS_LABELS[status] || status || '-';
}

export function statusTag(status) {
  return el('span', { class: `tag ${STATUS_CLASS[status] || ''}`, text: statusLabel(status) });
}

export function confidenceTag(confidence) {
  const map = { high: ['高', 'tag-success'], medium: ['中', 'tag-warning'], low: ['低', 'tag-danger'] };
  const [label, cls] = map[confidence] || ['中', 'tag-warning'];
  return el('span', { class: `tag ${cls} conf`, text: `置信度 ${label}` });
}

/* ---------------------------------------------------------------- 提示条 */

export function toast(message, kind = '') {
  let host = document.getElementById('toasts');
  if (!host) {
    host = el('div', { id: 'toasts' });
    document.body.append(host);
  }
  const node = el('div', { class: `toast ${kind}`, text: message });
  host.append(node);
  setTimeout(() => node.remove(), kind === 'err' ? 6000 : 3200);
}

export const toastOk = (m) => toast(m, 'ok');
export const toastErr = (m) => toast(m, 'err');

/* ---------------------------------------------------------------- 剪贴板 */

export async function copyText(text) {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch (_) {
    // 非 HTTPS / 无权限时退化为 execCommand
    const area = el('textarea', { style: 'position:fixed;opacity:0;' });
    area.value = text;
    document.body.append(area);
    area.select();
    let ok = false;
    try { ok = document.execCommand('copy'); } catch (_e) { ok = false; }
    area.remove();
    return ok;
  }
}

/** 自动增高、Ctrl/Cmd+Enter 发送的文本域 */
export function bindComposer(textarea, onSend) {
  const resize = () => {
    textarea.style.height = 'auto';
    textarea.style.height = `${Math.min(textarea.scrollHeight, 150)}px`;
  };
  textarea.addEventListener('input', resize);
  textarea.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && event.key === 'Enter') {
      event.preventDefault();
      onSend();
    }
  });
  resize();
}
