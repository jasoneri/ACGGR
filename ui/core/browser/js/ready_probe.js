(function(){
const previewRuntime = window.previewRuntime;
const previewCommandBus = window.previewCommandBus;
const readyState = document.readyState;
return JSON.stringify({
  domReady: readyState === 'interactive' || readyState === 'complete',
  readyState,
  hasPreviewRuntime: Boolean(
    previewRuntime
    && typeof previewRuntime.scanChecked === 'function'
    && typeof previewRuntime.markDownloaded === 'function'
  ),
  hasScanChecked: typeof window.scanChecked === 'function'
    || Boolean(previewRuntime && typeof previewRuntime.scanChecked === 'function'),
  hasCollectPreviewSubmitPayload: typeof window.collectPreviewSubmitPayload === 'function',
  hasPreviewCommandBus: Boolean(previewCommandBus && typeof previewCommandBus.dispatch === 'function')
});
})();