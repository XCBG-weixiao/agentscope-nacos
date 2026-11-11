const snippet = document.querySelector('#snippet');
const copy = document.querySelector('#copy');
const copyStatus = document.querySelector('#copy-status');
snippet.textContent = `<script defer src="${location.origin}/embed.js"></script>`;
copy.hidden = !navigator.clipboard;
copy.addEventListener('click', async () => {
  try {
    await navigator.clipboard.writeText(snippet.textContent);
    copyStatus.textContent = '已复制';
  } catch {
    copyStatus.textContent = '浏览器未授予剪贴板权限，可选中代码复制。';
  }
  copyStatus.hidden = false;
});
