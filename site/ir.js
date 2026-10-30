// NINA's IR reader: pick the board, press the remote, read the codes.
(function () {
  const box = document.getElementById('irBox');
  const port = document.getElementById('irPort');
  const pin = document.getElementById('irPin');
  const note = document.getElementById('irNote');
  const codes = document.getElementById('irCodes');
  const log = document.getElementById('irLog');
  const start = document.getElementById('irStart');
  const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
  const post = (path, body) => fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json'}, body: JSON.stringify(body)});

  function ports() {
    fetch('/status').then(r => r.json()).then(d => {
      const list = Object.keys(d.ports || {});
      const was = port.value;
      port.innerHTML = list.map(p => `<option>${esc(p)}</option>`).join('');
      if (list.includes(was)) port.value = was;
      else { const usb = list.find(p => /usb|arduino|ch340/i.test(d.ports[p])); if (usb) port.value = usb; }
    }).catch(() => {});
  }

  function show() {
    fetch('/ir').then(r => r.json()).then(d => {
      start.textContent = d.port ? 'STOP READING' : 'START READING';
      note.textContent = d.error || (d.flashing ? 'Flashing the reader...' : d.port
        ? (d.bytes ? `Reading ${d.port}: press a button` : `Reading ${d.port}, nothing's arrived yet: press reset on the board, then a button`)
        : 'Not reading');
      note.className = d.error ? 'ir-bad' : '';
      if (d.who && d.port && !d.error && !d.flashing) note.textContent = `Shook hands on ${d.port}: "I am ${d.who}". Press a button.`;
      const found = Object.entries(d.boards || {});
      document.getElementById('irBoards').textContent = d.asking ? 'Asking the boards...'
        : found.map(([p, who]) => `${p}: ${who}`).join(' · ');
      codes.innerHTML = d.codes.slice().reverse().map(c => `<b>${esc(c.code)}</b>`).join('') || '<span>No codes yet</span>';
      log.textContent = d.lines.join('\n');
    }).catch(() => {});
  }

  start.addEventListener('click', () => {
    (start.textContent === 'STOP READING' ? post('/ir/stop', {}) : post('/ir/start', {port: port.value})).then(show);
  });
  document.getElementById('irWho').addEventListener('click', () => {
    if (confirm("Ask every free board who it is? It resets each one, so don't do it while a robot's moving.")) post('/ir/boards', {}).then(show);
  });
  document.getElementById('irFlash').addEventListener('click', () => {
    if (confirm(`Replace what's on the board on ${port.value} with the IR reader, receiver on pin ${pin.value}?`)) {
      post('/ir/flash', {port: port.value, pin: pin.value}).then(show);
    }
  });

  ports();
  show();
  setInterval(ports, 5000);
  setInterval(() => { if (!document.hidden && box) show(); }, 700);
})();
