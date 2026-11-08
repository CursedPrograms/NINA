// NINA's two talk switches (/talkmute; RIFT has the same): her beeps when she chats, and the chatting itself
(function () {
  const sound = document.getElementById('talkBtn'), chat = document.getElementById('chatBtn');
  function show(d) {
    if (d.talk_muted === undefined) return;
    sound.textContent = d.talk_muted ? 'Chat sound: off' : 'Chat sound: on';
    sound.classList.toggle('muted', d.talk_muted);
    chat.textContent = d.chat_off ? 'Fleet chat: off' : 'Fleet chat: on';
    chat.classList.toggle('muted', d.chat_off);
  }
  function flip(which) {
    fetch('/talkmute?' + which + '=toggle').then(r => r.json()).then(show).catch(() => {});
  }
  function load() { fetch('/talkmute').then(r => r.json()).then(show).catch(() => {}); }
  sound.addEventListener('click', () => flip('on'));
  chat.addEventListener('click', () => flip('chat_off'));
  load();
  setInterval(load, 15000);
})();
