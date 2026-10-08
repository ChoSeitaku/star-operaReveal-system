/**
 * 认证回调模块：授权跳转、回调参数接收、登录态校验、退出登录。
 *
 * 说明：授权码换取访问令牌由后端 /auth/callback 在服务端完成（避免 client_secret
 * 暴露在浏览器），浏览器侧只负责接收回调结果、校验登录态并跳转工作台。
 */
import { LOGIN_URL, getJSON, postJSON } from './api.js';

export const ROLE_LABELS = { admin: '系统管理员', analyst: '分析用户' };

export function roleLabel(role) {
  return ROLE_LABELS[role] || role || '-';
}

/** 跳转认证中心发起授权 */
export function startLogin() {
  window.location.href = LOGIN_URL;
}

/** 退出登录并回到入口页 */
export async function logout() {
  try {
    await postJSON('/auth/logout');
  } catch (_) { /* 即使失败也继续跳转 */ }
  window.location.href = '/';
}

/** 获取当前用户；未登录时返回 null（不跳转） */
export async function currentUser() {
  try {
    return await getJSON('/api/users/me');
  } catch (_) {
    return null;
  }
}

/**
 * 查询登录态，始终返回 200，供入口页 / 回调页等公开页面使用，
 * 避免未登录时的 401 在浏览器控制台留下无意义的错误记录。
 */
export async function sessionState() {
  try {
    return await getJSON('/api/session');
  } catch (_) {
    return { authenticated: false, user: null };
  }
}

/** 工作台等受限页面使用：未登录则跳转登录入口 */
export async function requireLogin() {
  const user = await currentUser();
  if (!user) {
    startLogin();
    return null;
  }
  return user;
}

/** 解析 /callback.html?status=&message=&display_name=&role= */
export function parseCallbackParams() {
  const params = new URLSearchParams(window.location.search);
  return {
    status: params.get('status') || '',
    message: params.get('message') || '',
    displayName: params.get('display_name') || '',
    role: params.get('role') || '',
  };
}
