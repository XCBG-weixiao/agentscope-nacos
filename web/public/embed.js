(() => {
  const source = new URL(document.currentScript.src);
  if (document.querySelector('[data-friday-widget]')) return;

  const host = document.createElement('div');
  host.dataset.fridayWidget = '';
  host.hidden = true;
  const shadow = host.attachShadow({ mode: 'open' });

  const style = document.createElement('link');
  style.rel = 'stylesheet';
  style.href = new URL('embed.css', source);
  style.addEventListener('load', () => { host.hidden = false; });

  const launcher = document.createElement('button');
  launcher.className = 'launcher';
  launcher.type = 'button';
  launcher.setAttribute('aria-expanded', 'false');
  launcher.setAttribute('aria-controls', 'friday-panel');
  const icon = document.createElement('span');
  icon.className = 'bubble';
  icon.setAttribute('aria-hidden', 'true');
  icon.textContent = '•••';
  launcher.append(icon, '问 Friday');

  const frame = document.createElement('iframe');
  frame.id = 'friday-panel';
  frame.title = '与 Friday 对话';
  frame.hidden = true;

  function toggle(open) {
    if (open && !frame.hasAttribute('src')) frame.src = new URL('widget/', source);
    frame.hidden = !open;
    launcher.setAttribute('aria-expanded', String(open));
    if (open) frame.focus();
    else launcher.focus();
  }

  launcher.addEventListener('click', () => toggle(frame.hidden));
  window.addEventListener('message', event => {
    if (event.origin === source.origin && event.source === frame.contentWindow &&
        event.data === 'friday:close') toggle(false);
  });
  shadow.append(style, frame, launcher);
  document.body.append(host);
})();
