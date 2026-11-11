const conversation = document.querySelector('#conversation');
const welcome = document.querySelector('#welcome');
const messages = document.querySelector('#messages');
const status = document.querySelector('#status');
const error = document.querySelector('#error');
const composer = document.querySelector('#composer');
const input = document.querySelector('#message');
const send = document.querySelector('#send');
const reset = document.querySelector('#reset');
const close = document.querySelector('#close');
const session = document.querySelector('#session');
const context = document.querySelector('#context');
let contextId;

function appendMessage(role, label, text) {
  const item = document.createElement('li');
  item.className = `message ${role}`;
  item.setAttribute('aria-label', `${label}：${text}`);
  item.textContent = text;
  messages.append(item);
  return item;
}

function setBusy(busy) {
  send.disabled = busy;
  reset.disabled = busy;
  input.readOnly = busy;
  status.hidden = !busy;
  status.textContent = busy ? 'Friday 正在回复…' : '';
}

composer.addEventListener('submit', async event => {
  event.preventDefault();
  const message = input.value.trim();
  if (!message || send.disabled) return;

  error.hidden = true;
  welcome.hidden = true;
  const pending = appendMessage('user', '你', message);
  input.value = '';
  setBusy(true);
  conversation.scrollTop = conversation.scrollHeight;
  try {
    const response = await fetch('/api/chat', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message, contextId }),
    });
    const result = await response.json();
    if (!response.ok) throw new Error(result.error);
    contextId = result.contextId;
    context.textContent = contextId;
    session.hidden = false;
    appendMessage('assistant', 'Friday', result.reply);
  } catch (failure) {
    pending.remove();
    welcome.hidden = messages.childElementCount > 0;
    input.value = message;
    error.textContent = failure instanceof TypeError ? '无法连接聊天服务，请重试。' : failure.message;
    error.hidden = false;
  } finally {
    setBusy(false);
    conversation.scrollTop = conversation.scrollHeight;
    if (document.activeElement === send || document.activeElement === document.body) input.focus();
  }
});

input.addEventListener('keydown', event => {
  if (event.key === 'Enter' && !event.shiftKey && !event.isComposing) {
    event.preventDefault();
    composer.requestSubmit();
  }
});
document.querySelector('.suggestion').addEventListener('click', event => {
  input.value = event.currentTarget.textContent;
  input.focus();
});
reset.addEventListener('click', () => {
  contextId = undefined;
  context.textContent = '';
  session.hidden = true;
  session.open = false;
  error.hidden = true;
  input.value = '';
  messages.replaceChildren();
  welcome.hidden = false;
  input.focus();
});

if (window.parent === window) close.hidden = true;
close.addEventListener('click', () => window.parent.postMessage('friday:close', '*'));
document.addEventListener('keydown', event => {
  if (event.key === 'Escape' && window.parent !== window) close.click();
});
