(() => {
  const wantDark = __ACGG_DARK__;
  const want = wantDark ? 'dark' : 'light';
  const KEY = 'AriaNg.Options';
  // Every real AriaNg settings object carries both; a partial one cannot
  // be completed from here because its other keys are the page's own.
  const isComplete = (o) => !!(o && typeof o === 'object' && !Array.isArray(o)
      && o.rpcHost !== undefined && o.protocol !== undefined);
  const readOptions = () => {
    try {
      const raw = localStorage.getItem(KEY);
      return raw === null ? null : JSON.parse(raw);
    } catch (e) { return null; }
  };
  const paint = () => {
    try {
      const body = document.body;
      if (body) { body.classList.toggle('theme-dark', wantDark); }
    } catch (e) {}
  };
  const boot = () => {
    try {
      const raw = localStorage.getItem(KEY);
      if (raw === null) { return; }  // absent: AriaNg writes its own defaults
      const opt = readOptions();
      if (!isComplete(opt)) { localStorage.removeItem(KEY); return; }
      if (opt.theme !== want) {
        opt.theme = want;
        localStorage.setItem(KEY, JSON.stringify(opt));
      }
    } catch (e) {}
  };
  boot();
  paint();
  try { if (window.__acggrThemePoll) { clearTimeout(window.__acggrThemePoll); } } catch (e) {}
  window.__acggrThemePoll = 0;
  const settle = (tries) => {
    window.__acggrThemePoll = 0;
    let booted = false;
    try {
      booted = !!(window.angular && document.body
          && angular.element(document.body).injector());
    } catch (e) { booted = false; }
    if (!booted) {
      if (tries > 0) { window.__acggrThemePoll = setTimeout(() => settle(tries - 1), 100); }
      return;
    }
    try {
      const opt = readOptions();
      if (isComplete(opt) && opt.theme !== want) {
        opt.theme = want;
        localStorage.setItem(KEY, JSON.stringify(opt));
      }
    } catch (e) {}
    paint();
  };
  window.__acggrThemePoll = setTimeout(() => settle(50), 100);
})();
