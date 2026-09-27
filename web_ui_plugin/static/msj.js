const filter = document.querySelector('#filter');
filter?.addEventListener('input', () => {
  let visible = 0;
  document.querySelectorAll('[data-search]').forEach(card => {
    card.hidden = !card.dataset.search.toLocaleLowerCase().includes(filter.value.trim().toLocaleLowerCase());
    if (!card.hidden) visible++;
  });
  document.querySelector('#no-results').hidden = visible > 0;
});
let photoUrl;
document.querySelector('#photo')?.addEventListener('change', event => {
  const file = event.target.files[0];
  if (!file) return;
  if (file.size > 8 * 1024 * 1024) { alert('Choose a photo smaller than 8 MB.'); event.target.value = ''; return; }
  if (photoUrl) URL.revokeObjectURL(photoUrl);
  photoUrl = URL.createObjectURL(file);
  const preview = document.querySelector('#photo-preview');
  preview.src = photoUrl; preview.hidden = false;
  const placeholder = document.querySelector('#upload-placeholder');
  if (placeholder) placeholder.hidden = true;
  const remove = document.querySelector('[name=remove_photo]');
  if (remove) remove.checked = false;
});
document.querySelectorAll('[data-confirm]').forEach(form => form.addEventListener('submit', event => {
  if (!confirm(form.dataset.confirm)) event.preventDefault();
}));
