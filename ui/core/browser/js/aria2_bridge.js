(function () {
__ACGG_QWEBCHANNEL__
  var PORT = __ACGG_PORT__;
  var READY = __ACGG_READY__;
  var DIR = __ACGG_DIR__;
  var HOST_OWNS_DIR = __ACGG_HOST_OWNS_DIR__;
  var seq = 0;
  var pending = {};
  var openDirWaiters = {};
  var tagWaiters = {};
  var bridge = null;
  var waiters = [];
  var failed = false;
  // Distinct from ``failed``: true only when there is no QWebChannel/qt at
  // all. "No engine" must not read as "no channel" -- the history channel
  // has to work while aria2 is down (that is its whole point).
  var channelFailed = false;
  var historyWaiters = {};
  var historyRows = [];
  function fail(message) {
    var err = new Error(message);
    err.code = -1;
    return err;
  }
  function settle(err) {
    failed = true;
    for (var id in pending) {
      var entry = pending[id];
      delete pending[id];
      entry.reject(err);
    }
    for (var did in openDirWaiters) {
      var dentry = openDirWaiters[did];
      delete openDirWaiters[did];
      dentry.reject(err);
    }
    for (var tid in tagWaiters) {
      var tentry = tagWaiters[tid];
      delete tagWaiters[tid];
      tentry.reject(err);
    }
    for (var hid in historyWaiters) {
      var hentry = historyWaiters[hid];
      delete historyWaiters[hid];
      hentry.reject(err);
    }
    var waiting = waiters;
    waiters = [];
    for (var i = 0; i < waiting.length; i++) { waiting[i].reject(err); }
  }
  function deliver(requestId, payloadJson) {
    var entry = pending[requestId];
    if (!entry) { return; }
    delete pending[requestId];
    var payload;
    try { payload = JSON.parse(payloadJson); }
    catch (e) { entry.reject(fail('acggr_bridge_bad_payload')); return; }
    if (payload && payload.ok) { entry.resolve(payload.result); return; }
    var err = new Error((payload && payload.error && payload.error.message) || 'aria2_error');
    err.code = payload && payload.error ? payload.error.code : -1;
    entry.reject(err);
  }
  function rpc(method, params) {
    return new Promise(function (resolve, reject) {
      if (failed) { reject(fail('aria2_unavailable')); return; }
      // Host owns the download dir: even a direct rpc('aria2.addUri', [...]) call
      // (bypassing the addUri() wrapper) must land in DIR — safety net for stale
      // bundles still carrying an old dir. Only touch the well-known shape; never
      // invent an object or swallow errors.
      if (HOST_OWNS_DIR && DIR && method === 'aria2.addUri'
          && Array.isArray(params) && params[1]
          && typeof params[1] === 'object' && !Array.isArray(params[1])) {
        params[1].dir = DIR;
      }
      whenReady().then(function (b) {
        var id = 'acggr-' + (++seq);
        pending[id] = {resolve: resolve, reject: reject};
        try { b.submit(id, String(method), JSON.stringify(params || [])); }
        catch (e) { delete pending[id]; reject(fail('acggr_bridge_submit_failed')); }
      }, function (e) { reject(e); });
    });
  }
  // Channel handshake, without the engine-ready gate. ``whenReady()`` keeps
  // its old meaning exactly (rejects while the engine is unreachable); the
  // history path uses this one so a down engine does not also take the
  // history away.
  function whenChannel() {
    if (bridge) { return Promise.resolve(bridge); }
    if (channelFailed) { return Promise.reject(fail('acggr_bridge_missing')); }
    return new Promise(function (resolve, reject) { waiters.push({resolve: resolve, reject: reject}); });
  }
  function whenReady() {
    if (failed || !READY) { return Promise.reject(fail('aria2_unavailable')); }
    return whenChannel();
  }
  function addUri(uris, options) {
    var opts = {};
    if (options) { for (var k in options) { opts[k] = options[k]; } }
    if (HOST_OWNS_DIR) { if (DIR) { opts.dir = DIR; } }
    else if (!opts.dir) { opts.dir = DIR; }
    if (opts['continue'] === undefined) { opts['continue'] = 'false'; }
    return rpc('aria2.addUri', [Array.isArray(uris) ? uris : [uris], opts]);
  }
  // Open the folder holding a task's payload. The page passes a gid and
  // nothing else: the host resolves it through its own aria2.tellStatus,
  // takes files[0].path's dirname and opens that. A path string from the
  // page is never accepted, so the page cannot ask the host to open an
  // arbitrary location.
  var GID_RE = /^[0-9a-f]{16}$/;
  function openItemDir(gid) {
    return new Promise(function (resolve, reject) {
      var key = String(gid == null ? '' : gid);
      if (!GID_RE.test(key)) { reject(fail('acggr_bad_gid')); return; }
      if (failed) { reject(fail('aria2_unavailable')); return; }
      whenReady().then(function (b) {
        var id = 'acggr-dir-' + (++seq);
        openDirWaiters[id] = {resolve: resolve, reject: reject};
        try { b.openItemDir(key, id); }
        catch (e) { delete openDirWaiters[id]; reject(fail('acggr_bridge_submit_failed')); }
      }, function (e) { reject(e); });
    });
  }
  function deliverOpenDir(requestId, payloadJson) {
    var entry = openDirWaiters[requestId];
    if (!entry) { return; }
    delete openDirWaiters[requestId];
    var payload;
    try { payload = JSON.parse(payloadJson); }
    catch (e) { entry.reject(fail('acggr_bridge_bad_payload')); return; }
    if (payload && payload.ok) { entry.resolve(payload.result); return; }
    var err = new Error((payload && payload.error && payload.error.message) || 'open_dir_failed');
    err.code = payload && payload.error ? payload.error.code : -1;
    entry.reject(err);
  }
  // Tag one already-downloaded file. The page passes a bare file name, a
  // flat meta object and the dir it was told to use; the host resolves the
  // name against its own download dir and drops unknown meta keys. Same
  // request_id callback shape as rpc()/openItemDir(), no new protocol.
  function tagFile(filename, meta, dir) {
    return new Promise(function (resolve, reject) {
      var name = String(filename == null ? '' : filename);
      if (!name || name.charAt(0) === '/' || name.charAt(0) === '\\'
          || /^[A-Za-z]:/.test(name)) {
        reject(fail('acggr_bad_filename'));
        return;
      }
      var segs = name.replace(/\\/g, '/').split('/');
      for (var i = 0; i < segs.length; i++) {
        if (segs[i] === '..') { reject(fail('acggr_bad_filename')); return; }
      }
      if (failed) { reject(fail('aria2_unavailable')); return; }
      whenReady().then(function (b) {
        var id = 'acggr-tag-' + (++seq);
        tagWaiters[id] = {resolve: resolve, reject: reject};
        var metaJson = JSON.stringify(meta || {});
        var dirText = (dir === undefined || dir === null) ? DIR : String(dir);
        try { b.tagFile(name, metaJson, dirText, id); }
        catch (e) { delete tagWaiters[id]; reject(fail('acggr_bridge_submit_failed')); }
      }, function (e) { reject(e); });
    });
  }
  function deliverTag(requestId, payloadJson) {
    var entry = tagWaiters[requestId];
    if (!entry) { return; }
    delete tagWaiters[requestId];
    var payload;
    try { payload = JSON.parse(payloadJson); }
    catch (e) { entry.reject(fail('acggr_bridge_bad_payload')); return; }
    if (payload && payload.ok) { entry.resolve(payload.result); return; }
    var err = new Error((payload && payload.error && payload.error.message) || 'tag_failed');
    err.code = payload && payload.error ? payload.error.code : -1;
    entry.reject(err);
  }
  // Download history from acggr's own record.db. Same request_id + JSON
  // payload-over-signal transport as rpc()/openItemDir()/tagFile(), with its
  // own waiter map for the same reason those two have one: the answer is a
  // row list, not an aria2 result, so it must not be resolved by (or resolve)
  // the wrong handler.
  function requestHistory(limit) {
    return new Promise(function (resolve, reject) {
      if (channelFailed) { reject(fail('acggr_bridge_missing')); return; }
      whenChannel().then(function (b) {
        if (!b || typeof b.requestHistory !== 'function') {
          reject(fail('acggr_history_unavailable'));
          return;
        }
        var id = 'acggr-hist-' + (++seq);
        historyWaiters[id] = {resolve: resolve, reject: reject};
        var text = (limit === undefined || limit === null) ? '' : String(limit);
        try { b.requestHistory(id, text); }
        catch (e) { delete historyWaiters[id]; reject(fail('acggr_bridge_submit_failed')); }
      }, function (e) { reject(e); });
    });
  }
  function deliverHistory(requestId, payloadJson) {
    var entry = historyWaiters[requestId];
    if (!entry) { return; }
    delete historyWaiters[requestId];
    var payload;
    try { payload = JSON.parse(payloadJson); }
    catch (e) { entry.reject(fail('acggr_bridge_bad_payload')); return; }
    if (payload && payload.ok) {
      historyRows = Array.isArray(payload.rows) ? payload.rows : [];
      try { window.__acggrHistory.rows = historyRows; } catch (e) {}
      entry.resolve(historyRows);
      return;
    }
    var err = new Error((payload && payload.error && payload.error.message) || 'history_failed');
    err.code = payload && payload.error ? payload.error.code : -1;
    entry.reject(err);
  }
  window.__acggrAria2 = {
    __acggrBridge: true,
    ready: READY,
    dir: DIR,
    port: PORT,
    rpc: rpc,
    whenReady: whenReady,
    addUri: addUri,
    openItemDir: openItemDir,
    tagFile: tagFile
  };
  // Readable while the engine is down: both the page's history script and
  // a host-side probe (offscreen verification) use this one object, and
  // ``rows`` mirrors the last successful answer for synchronous readers.
  window.__acggrHistory = {get: requestHistory, rows: []};
  if (typeof QWebChannel === 'undefined' || typeof qt === 'undefined') {
    channelFailed = true;
    settle(fail('aria2_unavailable'));
    return;
  }
  new QWebChannel(qt.webChannelTransport, function (channel) {
    var obj = channel.objects.__ACGG_OBJECT_NAME__;
    if (!obj) { channelFailed = true; settle(fail('acggr_bridge_missing')); return; }
    obj.responseReady.connect(deliver);
    if (obj.openDirReady) { obj.openDirReady.connect(deliverOpenDir); }
    if (obj.tagReady) { obj.tagReady.connect(deliverTag); }
    if (obj.historyReady) { obj.historyReady.connect(deliverHistory); }
    bridge = obj;
    var queued = waiters;
    waiters = [];
    for (var i = 0; i < queued.length; i++) { queued[i].resolve(obj); }
  });
  // No engine: RPC rejects exactly as before, but the channel above stays
  // connected, so history still answers. That split is the point of this
  // script -- with force-save=false the stopped list is empty after every
  // restart, and record.db is where the history actually lives.
  if (!READY) { settle(fail('aria2_unavailable')); }
})();
