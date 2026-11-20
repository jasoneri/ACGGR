(() => {
  const MARK = 'acggr-open-dir';
  const GID_RE = /^[0-9a-f]{16}$/;
  // Deliberately re-entrant: no 'already installed' early return.
  // The old one-way guard made this script fire at most once per
  // document. DocumentCreation runs it before <body> exists, so the
  // MutationObserver attach threw, got swallowed by try/catch, and the
  // flag then blocked every later re-run (host refresh, engine restart,
  // route change) -- the button never appeared until a full reload.
  // Re-running is safe because every step is idempotent: the observer
  // and the interval each have their own guard, and reconcile() removes
  // duplicates instead of appending.
  function bridge() { return window.__acggrAria2 || null; }
  function validGid(row) {
    const gid = row && row.getAttribute('data-gid');
    return gid && GID_RE.test(gid) ? gid : null;
  }
  function makeButton() {
    const a = document.createElement('a');
    a.className = MARK + ' btn btn-default btn-xs';
    a.setAttribute('role', 'button');
    a.setAttribute('title', '打开下载物目录');
    a.setAttribute('aria-label', '打开下载物目录');
    a.style.position = 'absolute';
    a.style.top = '50%';
    a.style.transform = 'translateY(-50%)';
    a.style.right = '40px';
    a.style.zIndex = '3';
    a.style.padding = '1px 5px';
    a.style.lineHeight = '20px';
    a.innerHTML = '<i class="fa fa-folder-open"></i>';
    a.addEventListener('click', function (ev) {
      ev.preventDefault();
      ev.stopPropagation();
      const b = bridge();
      if (!b || typeof b.openItemDir !== 'function') { return; }
      const gid = validGid(a.parentElement);
      if (!gid) { return; }
      a.classList.add('acggr-open-dir-busy');
      Promise.resolve(b.openItemDir(gid)).catch(function (e) {
        // The host logs too; this keeps the page-side reason visible in
        // the WebEngine console instead of a silent no-op click.
        try { console.error('acggrAria2 openItemDir failed: ' + (e && e.message || e)); } catch (err) {}
      }).then(function () {
        a.classList.remove('acggr-open-dir-busy');
      });
    }, true);
    return a;
  }
  function reconcile(row) {
    if (!row || !row.isConnected) { return; }
    const gid = validGid(row);
    const own = row.querySelectorAll(':scope > .' + MARK);
    for (let i = gid ? 1 : 0; i < own.length; i++) { own[i].remove(); }
    if (!gid) { return; }
    if (own.length && own[0].isConnected) { return; }
    if (getComputedStyle(row).position === 'static') { row.style.position = 'relative'; }
    row.appendChild(makeButton());
  }
  function sweep() {
    const all = document.querySelectorAll('.' + MARK);
    for (let i = 0; i < all.length; i++) {
      const row = all[i].parentElement;
      if (!row || !row.isConnected || !validGid(row)) { all[i].remove(); }
    }
  }
  let scheduled = false;
  function scan() {
    scheduled = false;
    try {
      sweep();
      const rows = document.querySelectorAll('.task-table-body > div.row[data-gid]');
      for (let i = 0; i < rows.length; i++) { reconcile(rows[i]); }
    } catch (e) {}
  }
  function schedule() {
    if (scheduled) { return; }
    scheduled = true;
    setTimeout(scan, 0);
  }
  function start() {
    try {
      scan();
      // ``document.body`` can still be null when this runs at
      // DocumentCreation; observing null throws, and the old code then
      // had no observer for the rest of the document's life.
      if (!window.__acggrOpenDirObserver && document.body) {
        const obs = new MutationObserver(schedule);
        obs.observe(document.body, {childList: true, subtree: true});
        window.__acggrOpenDirObserver = obs;
      }
      if (!window.__acggrOpenDirTimer) {
        window.__acggrOpenDirTimer = setInterval(scan, 1500);
      }
    } catch (e) {}
  }
  window.__acggrOpenDirScanProbe = scan;
  if (document.readyState === 'loading' || !document.body) {
    // Retry on both events: DOMContentLoaded alone is missed when this
    // script arrives after it already fired (a host re-injection).
    document.addEventListener('DOMContentLoaded', start, {once: true});
    window.addEventListener('load', start, {once: true});
    // Last-resort poll: covers a re-injection into a document that has
    // already finished loading but whose body is momentarily gone.
    setTimeout(start, 300);
  } else {
    start();
  }
})();
