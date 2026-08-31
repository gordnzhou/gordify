(function () {
    const toggle = document.getElementById('help-toggle');
    const panel = document.getElementById('help-panel');
    const close = document.getElementById('help-close');

    function openPanel() {
      panel.classList.add('open');
      toggle.setAttribute('aria-expanded', 'true');
    }
    function closePanel() {
      panel.classList.remove('open');
      toggle.setAttribute('aria-expanded', 'false');
    }

    toggle.addEventListener('click', (e) => {
      e.stopPropagation();
      panel.classList.contains('open') ? closePanel() : openPanel();
    });
    close.addEventListener('click', closePanel);

    document.addEventListener('click', (e) => {
      if (!panel.contains(e.target) && e.target !== toggle) closePanel();
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closePanel();
    });
  })();
