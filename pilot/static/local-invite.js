(() => {
  const invite = new URLSearchParams(location.hash.slice(1)).get('local_invite');
  if (!invite || location.hostname !== '127.0.0.1') return;
  const field = document.querySelector('form[action="/signup"] input[name="invite"]');
  if (field && /^[A-Za-z0-9_-]{40,80}$/.test(invite)) {
    field.value = invite;
    history.replaceState(null, '', location.pathname + location.search);
  }
})();
