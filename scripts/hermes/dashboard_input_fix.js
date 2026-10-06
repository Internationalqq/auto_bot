/* Compatibility fix for agent-browser 0.26: CDP rejects null keyboard fields. */
(() => {
  const originalSend = WebSocket.prototype.send;
  let activeSocket;
  WebSocket.prototype.send = function (payload) {
    if (typeof payload === 'string') {
      try {
        const event = JSON.parse(payload);
        if (event.type === 'input_mouse' || event.type === 'input_keyboard') activeSocket = this;
        if (event.type === 'input_keyboard') {
          for (const field of ['key', 'code', 'text']) event[field] ??= '';
          payload = JSON.stringify(event);
        }
      } catch (_) { /* Preserve non-JSON traffic. */ }
    }
    return originalSend.call(this, payload);
  };
  const isViewport = () => document.activeElement?.tagName === 'CANVAS';
  function insert(text) {
    if (!activeSocket || activeSocket.readyState !== WebSocket.OPEN) return false;
    for (const character of text) {
      activeSocket.send(JSON.stringify({type: 'input_keyboard', eventType: 'keyDown', key: character, code: 'KeyA', text: character, windowsVirtualKeyCode: 65, modifiers: 0}));
      activeSocket.send(JSON.stringify({type: 'input_keyboard', eventType: 'keyUp', key: character, code: 'KeyA', text: '', windowsVirtualKeyCode: 65, modifiers: 0}));
    }
    return true;
  }
  window.addEventListener('paste', event => {
    if (!isViewport()) return;
    if (insert(event.clipboardData.getData('text/plain'))) {
      event.preventDefault(); event.stopImmediatePropagation();
    }
  }, true);
  window.addEventListener('keydown', event => {
    if (!isViewport()) return;
    if (event.isComposing || event.key === 'Process') event.stopImmediatePropagation();
    // Let the browser deliver its native paste event rather than swallowing it.
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === 'v') event.stopImmediatePropagation();
  }, true);
  window.addEventListener('compositionend', event => {
    if (isViewport() && event.data) insert(event.data);
  }, true);
})();
