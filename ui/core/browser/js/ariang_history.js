(() => {
  'use strict';
  var TAG = '[acggrHistory]';
  var POLL_MS = 100;
  var MAX_WAIT_MS = 15000;
  var FETCH_RETRY_MS = 200;
  var FETCH_RETRY_MAX = 25;

  function note() {
    try { console.info.apply(console, [TAG].concat(Array.prototype.slice.call(arguments))); } catch (e) {}
  }
  function warn() {
    try { console.warn.apply(console, [TAG].concat(Array.prototype.slice.call(arguments))); } catch (e) {}
  }

  // Per-document state, kept across re-injections so a second run neither
  // double-wraps the service nor throws away rows already fetched.
  var state = window.__acggrHistoryState;
  if (!state) {
    state = window.__acggrHistoryState = {
      svc: null, inj: null, rows: [], order: [], byGid: {}, identByGid: {},
      orig: {}, loaded: false, loading: false,
      // Last stopped list aria2 itself answered (history already removed):
      // the only live reference the sidebar counter can be de-duplicated
      // against. null = never observed, treated as "no live rows known".
      liveStopped: null
    };
  }

  function injector() {
    try {
      return (window.angular && document.body)
        ? window.angular.element(document.body).injector() : null;
    } catch (e) { return null; }
  }

  // identity_key is a 32-char md5 hex; its first 16 chars are a stable
  // lowercase 16-hex gid, which is the shape AriaNg's gid regexes accept.
  function gidOf(row) {
    var hex = String((row && row.identity_key) || '').toLowerCase().replace(/[^0-9a-f]/g, '');
    return hex.length >= 16 ? hex.slice(0, 16) : '';
  }

  function statusOf(row) {
    var status = String((row && row.status) || '');
    if (status === 'error' || status === 'removed') { return status; }
    return 'complete';
  }

  function totalOf(row) {
    var total = parseInt((row && row.total_bytes) || 0, 10);
    return (isFinite(total) && total > 0) ? total : 0;
  }

  // dir + name, forward slashes: AriaNg's getFileName() splits on '/', so the
  // list shows the file name instead of the whole path.
  function payloadPath(dir, name) {
    var base = String(dir || '').replace(/[\\/]+$/, '').replace(/\\/g, '/');
    var tail = String(name || '').replace(/\\/g, '/');
    if (!base) { return tail; }
    if (!tail) { return base; }
    return base + '/' + tail;
  }

  function toRawTask(row) {
    var gid = gidOf(row);
    if (!gid) { return null; }
    var dir = String((row && row.dir) || '');
    var out = String((row && row.out) || '');
    var uri = String((row && row.uri) || '');
    var total = totalOf(row);
    var length = String(total);
    return {
      gid: gid,
      status: statusOf(row),
      totalLength: length,
      completedLength: length,
      downloadSpeed: '0',
      uploadSpeed: '0',
      connections: '0',
      numSeeders: '0',
      seeder: 'false',
      dir: dir,
      errorCode: String((row && row.error_code) || '0') || '0',
      files: [{
        index: '1',
        path: payloadPath(dir, out),
        length: length,
        completedLength: length,
        selected: 'true',
        uris: uri ? [{uri: uri, status: 'used'}] : []
      }],
      acggrHistory: true,
      acggrFinishedAt: (row && row.finished_at) || 0
    };
  }

  function indexRows() {
    var byGid = {};
    var order = [];
    var identByGid = {};
    for (var i = 0; i < state.rows.length; i++) {
      var task = toRawTask(state.rows[i]);
      if (!task || byGid[task.gid]) { continue; }
      byGid[task.gid] = task;
      order.push(task.gid);
      identByGid[task.gid] = identityOf(state.rows[i].dir, state.rows[i].out);
    }
    state.byGid = byGid;
    state.order = order;
    state.identByGid = identByGid;
  }

  // A live task and a recorded row can be the same download: the history gid is
  // derived, so gid de-duplication alone would still list the same file twice
  // for anything that finished during this session. dir + file name is the
  // record's own identity (download_history.identity_key is built from it).
  function identityOf(dir, name) {
    var folder = String(dir || '').replace(/\\/g, '/').replace(/\/+$/, '').toLowerCase();
    var file = String(name || '').replace(/\\/g, '/').toLowerCase();
    return file ? folder + '|' + file : '';
  }

  function liveIdentity(task) {
    var files = (task && task.files) || [];
    var path = (files[0] && files[0].path) || '';
    var parts = String(path).split(/[\\/]/);
    return identityOf(task && task.dir, parts[parts.length - 1]);
  }

  // aria2's own results stay first (they are the live truth); history is
  // appended and de-duplicated by gid *and* by payload identity against them.
  function mergeStopped(live) {
    var merged = [];
    var seen = {};
    var seenIdent = {};
    var i;
    if (Array.isArray(live)) {
      for (i = 0; i < live.length; i++) {
        var item = live[i];
        if (item && item.gid) { seen[item.gid] = true; }
        var ident = liveIdentity(item);
        if (ident) { seenIdent[ident] = true; }
        merged.push(item);
      }
    }
    var order = state.order || [];
    var identByGid = state.identByGid || {};
    for (i = 0; i < order.length; i++) {
      var gid = order[i];
      if (seen[gid] || seenIdent[identByGid[gid]]) { continue; }
      seen[gid] = true;
      merged.push(state.byGid[gid]);
    }
    return merged;
  }

  // How many history rows the stopped list shows on top of aria2's own list:
  // merged length minus live length, computed with the very same mergeStopped()
  // (gid + dir/name) so the counter and the list can never disagree about what
  // counts as a duplicate.
  function historyExtra() {
    if (!state.loaded || !state.rows || !state.rows.length) { return 0; }
    var live = Array.isArray(state.liveStopped) ? state.liveStopped : [];
    var extra = mergeStopped(live).length - live.length;
    return extra > 0 ? extra : 0;
  }

  // numStopped only; every other counter keeps aria2's own value. Written as a
  // string because that is the shape of the rest of this payload (aria2 sends
  // numbers as JSON strings), and AriaNg compares it numerically.
  function adjustGlobalStat(payload) {
    if (!payload || payload.success !== true) { return payload; }
    var data = payload.data;
    if (!data || typeof data !== 'object') { return payload; }
    var extra = historyExtra();
    if (extra <= 0) { return payload; }
    var base = parseInt(data.numStopped, 10);
    if (!isFinite(base) || base < 0) { base = 0; }
    data.numStopped = String(base + extra);
    return payload;
  }

  // The sidebar's "Finished / Stopped (N)" is $scope.globalStat.numStopped,
  // which MainController fills from aria2SettingService.getGlobalStat. Measured
  // on the shipped 1.3.14 bundle: that method is getGlobalStat(callback, silent)
  // and the callback receives {success, data: {downloadSpeed, numActive,
  // numStopped, numWaiting, uploadSpeed}}. aria2TaskService -- the service
  // hooked above for the list -- has no getGlobalStat at all, so the counter
  // has to be corrected here.
  function hookGlobalStat() {
    if (state.orig.getGlobalStat) { return; }
    var statSvc = null;
    try { statSvc = state.inj ? state.inj.get('aria2SettingService') : null; } catch (e) { statSvc = null; }
    if (!statSvc || typeof statSvc.getGlobalStat !== 'function') { return; }
    state.orig.getGlobalStat = statSvc.getGlobalStat;
    statSvc.getGlobalStat = function (callback, silent) {
      // No callback: nothing to correct, so pass straight through.
      if (typeof callback !== 'function') {
        return state.orig.getGlobalStat.apply(this, arguments);
      }
      var wrapped = function (payload) {
        var fixed = payload;
        try { fixed = adjustGlobalStat(payload); }
        catch (e) { warn('numStopped adjust failed', e); fixed = payload; }
        return callback(fixed);
      };
      return state.orig.getGlobalStat.call(this, wrapped, silent);
    };
  }

  function historyTask(gid) {
    var key = String(gid == null ? '' : gid);
    return (key && state.byGid[key]) ? state.byGid[key] : null;
  }

  // AriaNg's own processing (taskName, completePercent, fileName, singleUrl),
  // so a merged task renders exactly like a real one.
  function process(task) {
    if (state.svc && typeof state.svc.processDownloadTasks === 'function') {
      state.svc.processDownloadTasks([task]);
    }
  }

  function hookList() {
    if (state.orig.getTaskList) { return; }
    var fn = state.svc.getTaskList;
    if (typeof fn !== 'function') { return; }
    state.orig.getTaskList = fn;
    state.svc.getTaskList = function (type, wholeInfo, callback, silent) {
      // Only the stopped list is ours; every other type (and every call that
      // carries no callback, which AriaNg makes too) goes straight through.
      if (type !== 'stopped' || typeof callback !== 'function') {
        return state.orig.getTaskList.apply(this, arguments);
      }
      var wrapped = function (result) {
        try {
          if (result && result.success && Array.isArray(result.data)) {
            // Keep aria2's own list before history is appended: the counter
            // fix needs exactly this side of the merge to de-duplicate against.
            state.liveStopped = result.data.slice();
            // Spliced in place: the payload also carries
            // context.requestWholeInfo, which the list controller reads before
            // it ever looks at data.
            result.data = mergeStopped(result.data);
          }
        } catch (e) { warn('merge into stopped list failed', e); }
        return callback(result);
      };
      return state.orig.getTaskList.call(this, type, wholeInfo, wrapped, silent);
    };
  }

  function hookStatus() {
    if (!state.orig.getTaskStatus && typeof state.svc.getTaskStatus === 'function') {
      state.orig.getTaskStatus = state.svc.getTaskStatus;
      state.svc.getTaskStatus = function (gid, callback, silent, wholeInfo) {
        var task = historyTask(gid);
        if (task && typeof callback === 'function') {
          try {
            process(task);
            callback({success: true, data: task});
            return;
          } catch (e) { warn('history getTaskStatus failed', e); }
        }
        return state.orig.getTaskStatus.apply(this, arguments);
      };
    }

    // Independent of the one above: the detail page opened from a row calls
    // this and never getTaskStatus.
    if (!state.orig.getTaskStatusAndBtPeers
        && typeof state.svc.getTaskStatusAndBtPeers === 'function') {
      state.orig.getTaskStatusAndBtPeers = state.svc.getTaskStatusAndBtPeers;
      state.svc.getTaskStatusAndBtPeers = function (gid, callback) {
        var task = historyTask(gid);
        if (task && typeof callback === 'function') {
          try {
            process(task);
            // Payload shape the detail controller reads: {success, task, peers?}.
            // A history task has no bittorrent section, so it has no peers.
            callback({success: true, task: task});
            return;
          } catch (e) { warn('history getTaskStatusAndBtPeers failed', e); }
        }
        return state.orig.getTaskStatusAndBtPeers.apply(this, arguments);
      };
    }
  }

  function digest() {
    try { if (state.inj) { state.inj.get('$rootScope').$applyAsync(); } } catch (e) {}
  }

  function kick() {
    try {
      var root = state.inj.get('$rootScope');
      root.$broadcast('acggrDownloadHistoryUpdated');
      root.$applyAsync();
    } catch (e) {}
    // AriaNg's list controllers poll getTaskList on downloadTaskRefreshInterval
    // (default 1 s), so the merge lands on their next tick by itself; these two
    // extra digests only spare a list that is already on screen from waiting
    // for a poll that is in flight. No invented refresh entry point.
    setTimeout(digest, 200);
    setTimeout(digest, 1000);
  }

  function load(tries) {
    if (state.loading) { return; }
    var api = window.__acggrHistory;
    if (!api || typeof api.get !== 'function') {
      if (tries > 0) { setTimeout(function () { load(tries - 1); }, FETCH_RETRY_MS); }
      else { warn('history channel unavailable, stopped list stays aria2-only'); }
      return;
    }
    state.loading = true;
    // No limit argument: the host's own history_limit setting is authoritative
    // (0 means "history off", and the host answers with an empty list).
    Promise.resolve(api.get()).then(function (rows) {
      state.loading = false;
      state.rows = Array.isArray(rows) ? rows : [];
      state.loaded = true;
      indexRows();
      kick();
      note('merged ' + state.rows.length + ' history rows into the stopped list');
    }, function (err) {
      state.loading = false;
      warn('history load failed', err);
    });
  }

  function boot() {
    var started = Date.now();
    function attempt() {
      try { if (window.__acggrHistoryPoll) { clearTimeout(window.__acggrHistoryPoll); } } catch (e) {}
      window.__acggrHistoryPoll = 0;
      var inj = injector();
      if (inj) {
        var svc = null;
        try { svc = inj.get('aria2TaskService'); } catch (e) { svc = null; }
        if (svc) {
          state.inj = inj;
          state.svc = svc;
          hookList();
          hookStatus();
          hookGlobalStat();
          if (!state.loaded) { load(FETCH_RETRY_MAX); }
          return;
        }
      }
      if (Date.now() - started > MAX_WAIT_MS) {
        warn('AriaNg never booted (' + MAX_WAIT_MS + ' ms), history merge disabled');
        return;
      }
      window.__acggrHistoryPoll = setTimeout(attempt, POLL_MS);
    }
    attempt();
  }

  // Diagnostic surface: lets a host-side check assert the merge and the gid
  // mapping without a live aria2 (see the offscreen verification).
  window.__acggrHistoryProbe = {
    installed: function () { return !!state.orig.getTaskList; },
    rows: function () { return state.rows; },
    index: function () { return state.byGid; },
    merge: function (list) { return mergeStopped(list); },
    // Counter surface: installed flag, live-vs-history extra, and the pure
    // payload fix, so a host-side check can assert the arithmetic without a
    // live aria2 (see the offscreen verification).
    statInstalled: function () { return !!state.orig.getGlobalStat; },
    liveStopped: function () { return state.liveStopped || []; },
    extraStopped: function () { return historyExtra(); },
    adjustGlobalStat: function (payload) { return adjustGlobalStat(payload); }
  };

  try { boot(); } catch (e) { warn('boot failed', e); }
})();