(() => {
  const field = document.getElementById("invite-link");
  if (!field) return;
  field.value = new URL(field.value, location.origin).href;
  const button = document.getElementById("copy-link");
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(field.value);
      button.textContent = "Copied";
    } catch {
      field.focus(); field.select();
      button.textContent = "Copy the selected link";
    }
  });
})();
