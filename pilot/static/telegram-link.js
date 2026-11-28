(() => {
  'use strict';
  const status = document.getElementById('link-status');
  if (!status) return;
  const waitingText = status.textContent;

  const command = document.getElementById('link-command');
  const copyStatus = document.getElementById('link-copy-status');
  document.getElementById('copy-link-command').addEventListener('click', async () => {
    try {
      await navigator.clipboard.writeText(command.value);
      copyStatus.textContent = 'Copied. Paste it into your private bot chat.';
    } catch {
      command.focus();
      command.select();
      copyStatus.textContent = 'The message is selected. Copy it and paste it into your private bot chat.';
    }
  });

  let checking = false;
  async function check() {
    if (checking || document.hidden) return;
    checking = true;
    try {
      const response = await fetch('/setup/telegram/status', {credentials:'same-origin', cache:'no-store'});
      if (response.redirected || response.status === 401 || response.headers.get('X-Finder-Access') === 'paused') {
        location.reload();
        return;
      }
      if (!response.ok) throw new Error();
      const state = await response.json();
      if (!state.linked && !state.pending) location.replace('/setup/telegram?renew=1');
      else if (state.linked || state.pending !== status.dataset.link) location.reload();
      else status.textContent = waitingText;
    } catch {
      status.textContent = 'Finder is reconnecting to check your Telegram link.';
    } finally {
      checking = false;
    }
  }
  window.addEventListener('focus', check);
  window.addEventListener('pageshow', check);
  document.addEventListener('visibilitychange', check);
  window.setInterval(check, 5000);
})();
