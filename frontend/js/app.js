/**
 * 聊天工作台：编排会话 / 聊天 / 附件 / 结果 / 实时任务五个模块。
 *
 * 布局：左侧会话列表，中间消息区 + 底部输入框，右侧附件侧栏与实时任务区、结果区。
 */
import { getJSON } from './api.js';
import { AttachmentPanel } from './attachment.js';
import { currentUser, logout, requireLogin, roleLabel, startLogin } from './auth.js';
import { ChatPanel } from './chat.js';
import { ResultPanel } from './result.js';
import { SessionPanel } from './session.js';
import { TaskPanel } from './logs.js';
import { ChatSocket } from './ws.js';
import { bindComposer, el, fmtTime, toastErr, toastOk, clear } from './components.js';

const state = {
  user: null,
  conversationId: null,
  socket: null,
  sending: false,
};

let sessions;
let chat;
let attachments;
let tasks;
let results;

/* ---------------------------------------------------------------- 初始化 */

async function boot() {
  state.user = await requireLogin();
  if (!state.user) return;
  renderUser();

  sessions = new SessionPanel(document.getElementById('conv-list'), {
    onSelect: (conversationId) => openConversation(conversationId),
  });
  chat = new ChatPanel(document.getElementById('chat-scroll'), {
    onTaskDone: handleTaskDone,
    onTaskStart: () => { tasks.reset(); results.reset(); },
  });
  attachments = new AttachmentPanel(
    document.getElementById('attach-list'),
    document.getElementById('upload-zone'),
    document.getElementById('file-input'),
    { onChange: () => sessions.load().catch(() => {}) },
  );
  attachments.bindEvents();
  tasks = new TaskPanel(document.getElementById('task-panel'), {
    onCancel: (taskId) => {
      if (state.socket && state.socket.cancelTask(taskId)) toastOk('已发送取消请求');
      else toastErr('长连接不可用，无法取消');
    },
  });
  results = new ResultPanel(document.getElementById('result-panel'));

  bindActions();
  bindTabs();
  chat.showEmpty();
  tasks.setConnection('closed');
  results.reset();

  await sessions.load();
  const list = sessions.conversations;
  if (list.length) openConversation(list[0].conversation_id);
}

function renderUser() {
  const box = document.getElementById('user-box');
  clear(box);
  const nodes = [
    el('span', { class: 'who', text: `${state.user.display_name}（${roleLabel(state.user.role)}）` }),
  ];
  if (state.user.role === 'admin') {
    nodes.push(el('a', { class: 'btn btn-sm', href: '/admin', text: '管理台' }));
  }
  nodes.push(el('button', { class: 'btn btn-sm btn-ghost', text: '退出', onclick: () => logout() }));
  box.append(...nodes);
}

function bindActions() {
  document.getElementById('new-conv').addEventListener('click', async () => {
    try {
      await sessions.create();
      toastOk('已新建会话');
    } catch (err) {
      toastErr(err.detail || '新建失败');
    }
  });

  const input = document.getElementById('composer-input');
  const sendBtn = document.getElementById('send-btn');
  const send = () => { sendMessage(); };

  bindComposer(input, send);
  sendBtn.addEventListener('click', send);
}

function bindTabs() {
  const tabs = Array.from(document.querySelectorAll('.tab'));
  for (const tab of tabs) {
    tab.addEventListener('click', () => {
      for (const other of tabs) {
        other.classList.toggle('active', other === tab);
      }
      for (const body of document.querySelectorAll('.tab-body')) {
        body.hidden = body.dataset.tab !== tab.dataset.tab;
      }
    });
  }
}

/* ---------------------------------------------------------------- 会话切换 */

async function openConversation(conversationId) {
  if (state.socket) { state.socket.close(); state.socket = null; }
  state.conversationId = conversationId;

  if (!conversationId) {
    sessions.setActive(null);
    chat.showEmpty();
    attachments.setConversation(null);
    tasks.reset();
    results.reset();
    setComposerEnabled(false);
    return;
  }

  sessions.setActive(conversationId);
  const conv = sessions.current();
  document.getElementById('conv-title').textContent = conv ? conv.title : '会话';

  setComposerEnabled(true);
  results.reset();
  tasks.reset();
  // 必须立即切换附件面板：若等历史消息加载完再切，用户在切换瞬间上传的文件
  // 会被记到上一个会话名下
  attachments.setConversation(conversationId);

  try {
    const history = await getJSON(`/api/chat/ls/${conversationId}`);
    // 加载期间用户可能又切走了，丢弃过期响应
    if (state.conversationId !== conversationId) return;
    chat.replay(history.messages || []);

    // 恢复最近一次任务的结果与状态
    const withTask = (history.messages || []).filter((m) => m.task);
    const latest = withTask[withTask.length - 1];
    if (latest) {
      tasks.setTask(latest.task);
      tasks.loadLogs(latest.task.task_id);
      // 只有确实已产出结构化结果才请求，避免无意义的 404
      if (latest.task.has_result) await results.load(latest.task.task_id);
    }
  } catch (err) {
    toastErr(err.detail || '历史消息加载失败');
  }

  connectSocket(conversationId);
}

function setComposerEnabled(enabled) {
  document.getElementById('composer-input').disabled = !enabled;
  document.getElementById('send-btn').disabled = !enabled;
}

/* ---------------------------------------------------------------- 长连接 */

function connectSocket(conversationId) {
  state.socket = new ChatSocket(conversationId, {
    onState: (name, a, b) => {
      tasks.setConnection(name, a ?? b);
      if (name === 'giveup') toastErr('长连接不可用，请刷新页面重试');
    },
    onEvent: (event) => {
      if (event.conversation_id && event.conversation_id !== state.conversationId) return;
      chat.handleEvent(event);
      tasks.applyEvent(event);
      if (event.type === 'error') toastErr(event.error_message || '分析出错');
    },
  });
  state.socket.connect();
}

/* ---------------------------------------------------------------- 发送 */

/** 等待长连接就绪（刚切换会话时握手可能尚未完成） */
async function ensureSocketReady(timeoutMs = 8000) {
  if (state.socket && state.socket.isOpen()) return true;
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    await new Promise((resolve) => setTimeout(resolve, 150));
    if (state.socket && state.socket.isOpen()) return true;
  }
  return false;
}

async function sendMessage() {
  const input = document.getElementById('composer-input');
  const sendBtn = document.getElementById('send-btn');
  const content = input.value.trim();
  if (!content || state.sending) return;
  if (!state.conversationId) { toastErr('请先选择或新建一个会话'); return; }

  state.sending = true;
  sendBtn.disabled = true;
  try {
    // 连接未就绪时先等待，避免用户的消息被静默丢弃
    if (!(await ensureSocketReady())) {
      toastErr('长连接未就绪，请稍后重试');
      return;
    }

    const attachmentIds = attachments.parsedIds();
    if (!state.socket.sendMessage(content, attachmentIds)) {
      toastErr('发送失败，请重试');
      return;
    }

    chat.appendUserMessage(content, new Date().toISOString(),
      attachments.items.filter((i) => attachmentIds.includes(i.attachment_id)));
    input.value = '';
    input.style.height = 'auto';
  } finally {
    state.sending = false;
    sendBtn.disabled = false;
  }
}

/* ---------------------------------------------------------------- 结果回收 */

async function handleTaskDone(info) {
  // 最终消息与结果都以服务端为准，这里统一回放一次，保证展示一致
  try {
    const history = await getJSON(`/api/chat/ls/${state.conversationId}`);
    const messages = history.messages || [];
    const lastAssistant = [...messages].reverse().find((m) => m.role === 'assistant');
    // 仅当我们没有在流式渲染时（例如断线重连后）才整体回放，避免闪烁
    if (!chat.isStreaming() && lastAssistant) {
      const rendered = document.getElementById('chat-scroll').textContent || '';
      if (!rendered.includes(lastAssistant.content.slice(0, 24))) {
        chat.replay(messages);
      }
    }
    if (info.taskId) {
      tasks.syncFromServer(info.taskId);
      tasks.loadLogs(info.taskId);
      if (info.kind === 'result' || info.kind === 'done') await results.load(info.taskId);
    }
  } catch (_) { /* 忽略回收失败 */ }

  sessions.load().catch(() => {});
  attachments.refresh();
}

boot().catch((err) => {
  console.error(err);
  toastErr(`工作台初始化失败：${err.message}`);
});
