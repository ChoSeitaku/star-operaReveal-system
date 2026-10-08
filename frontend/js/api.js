/**
 * REST 调用封装：统一携带登录 Cookie、统一 401 跳登录、统一错误对象。
 */

export const LOGIN_URL = '/auth/login';

export class ApiError extends Error {
  constructor(status, detail) {
    super(detail || `HTTP ${status}`);
    this.name = 'ApiError';
    this.status = status;
    this.detail = detail;
  }
}

/** 当前页面路径是否属于公开页（401 时不跳转登录） */
function isPublicPage() {
  const p = window.location.pathname;
  return p === '/' || p.startsWith('/index') || p.startsWith('/callback');
}

async function request(path, { method = 'GET', body, headers = {}, raw = false } = {}) {
  const opts = { method, credentials: 'same-origin', headers: { ...headers } };

  if (body !== undefined && body !== null) {
    if (body instanceof FormData) {
      opts.body = body;
    } else {
      opts.body = JSON.stringify(body);
      opts.headers['content-type'] = 'application/json';
    }
  }

  const res = await fetch(path, opts);

  if (res.status === 401) {
    if (!isPublicPage()) window.location.href = LOGIN_URL;
    throw new ApiError(401, '未登录或登录已过期');
  }
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const data = await res.json();
      if (data && data.detail) detail = data.detail;
    } catch (_) { /* 非 JSON 响应，保留默认信息 */ }
    throw new ApiError(res.status, detail);
  }
  if (raw) return res;
  if (res.status === 204) return null;

  const contentType = res.headers.get('content-type') || '';
  if (contentType.includes('application/json')) return res.json();
  return res.text();
}

export const getJSON = (path) => request(path);
export const postJSON = (path, body) => request(path, { method: 'POST', body });

/** 带上传进度的文件上传（XHR，fetch 无法获取上传进度） */
export function uploadFile(conversationId, file, onProgress) {
  return new Promise((resolve, reject) => {
    const form = new FormData();
    form.append('conversation_id', String(conversationId));
    form.append('file', file);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/api/attachment/upload');
    xhr.withCredentials = true;

    xhr.upload.onprogress = (event) => {
      if (onProgress && event.lengthComputable) onProgress(event.loaded / event.total);
    };
    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        try {
          resolve(JSON.parse(xhr.responseText));
        } catch (_) {
          reject(new ApiError(0, '上传响应解析失败'));
        }
        return;
      }
      if (xhr.status === 401) {
        if (!isPublicPage()) window.location.href = LOGIN_URL;
        reject(new ApiError(401, '未登录或登录已过期'));
        return;
      }
      let detail = `HTTP ${xhr.status}`;
      try {
        detail = JSON.parse(xhr.responseText).detail || detail;
      } catch (_) { /* ignore */ }
      reject(new ApiError(xhr.status, detail));
    };
    xhr.onerror = () => reject(new ApiError(0, '网络错误，上传失败'));
    xhr.send(form);
  });
}

/** 触发浏览器下载（用于附件下载与结果导出） */
export function triggerDownload(url, filename) {
  const a = document.createElement('a');
  a.href = url;
  if (filename) a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
}
