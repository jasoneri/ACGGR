(() => {
  const want = __ACGG_LANG__;
  const KEY = 'AriaNg.Options';
  const isComplete = (o) => !!(o && typeof o === 'object' && !Array.isArray(o)
      && o.rpcHost !== undefined && o.protocol !== undefined);
  const readOptions = () => {
    try {
      const raw = localStorage.getItem(KEY);
      return raw === null ? null : JSON.parse(raw);
    } catch (e) { return null; }
  };
  const injector = () => {
    try { return (window.angular && document.body)
        ? angular.element(document.body).injector() : null; } catch (e) { return null; }
  };
  const boot = () => {
    try {
      const raw = localStorage.getItem(KEY);
      if (raw === null) { return; }  // absent: AriaNg writes its own defaults
      const opt = readOptions();
      if (!isComplete(opt)) { localStorage.removeItem(KEY); return; }
      if (opt.language !== want) {
        opt.language = want;
        localStorage.setItem(KEY, JSON.stringify(opt));
      }
    } catch (e) {}
  };
  boot();
  try { if (window.__acggrLangPoll) { clearTimeout(window.__acggrLangPoll); } } catch (e) {}
  window.__acggrLangPoll = 0;
  const apply = (inj) => {
    try {
      // setLanguage persists the choice; $translate.use repaints the DOM.
      inj.get('ariaNgSettingService').setLanguage(want);
      inj.get('$translate').use(want);
    } catch (e) {}
  };
  const settle = (tries) => {
    window.__acggrLangPoll = 0;
    const inj = injector();
    if (!inj) {
      if (tries > 0) { window.__acggrLangPoll = setTimeout(() => settle(tries - 1), 100); }
      return;
    }
    apply(inj);
  };
  settle(50);
})();
