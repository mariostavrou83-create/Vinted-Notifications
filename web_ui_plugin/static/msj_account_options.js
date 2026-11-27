for (const details of document.querySelectorAll('[data-account-options]')) {
  const dialog = document.createElement('dialog');
  if (typeof dialog.showModal !== 'function') continue;
  const summary = details.querySelector('summary');
  const body = details.querySelector('.account-options-body');
  const close = document.createElement('button');
  close.type = 'button';
  close.className = 'secondary';
  close.textContent = 'Close';
  dialog.className = 'account-options-dialog';
  dialog.setAttribute('aria-labelledby', body.querySelector('h3').id);
  summary.setAttribute('aria-haspopup', 'dialog');
  details.after(dialog);
  summary.addEventListener('click', event => {
    event.preventDefault();
    dialog.append(body, close);
    dialog.showModal();
    close.focus();
  });
  close.addEventListener('click', () => dialog.close());
  dialog.addEventListener('close', () => {
    details.append(body);
    summary.focus();
  });
}

for (const time of document.querySelectorAll('time[data-home-check]')) {
  const date = new Date(Number(time.dataset.homeCheck) * 1000);
  if (!Number.isFinite(date.getTime())) continue;
  time.dateTime = date.toISOString();
  const formatter = new Intl.DateTimeFormat('en-GB', {
    day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit', timeZone: 'Europe/London'
  });
  time.textContent = ' · ' + formatter.format(date) + ' UK time';
}
