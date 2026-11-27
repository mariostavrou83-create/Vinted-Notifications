(() => {
  const params = new URLSearchParams(location.hash.slice(1));
  const local = location.hostname === "127.0.0.1";
  const invite = (location.protocol === "https:" || local) && params.get("invite")
    || (local && params.get("local_invite"));
  if (!invite) return;
  history.replaceState(null, "", location.pathname + location.search);
  const field = document.querySelector('form[action="/signup"] input[name="invite"]');
  if (field && /^[A-Za-z0-9_-]{32,100}$/.test(invite)) field.value = invite;
})();
