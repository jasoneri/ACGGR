(function(){
let __acggValue = null;
let __acggKind = "undefined";
try {
  __acggValue = (function(){
__ACGG_JS_BODY__
  })();
  if (Array.isArray(__acggValue)) {
    __acggKind = "array";
  } else if (__acggValue === null) {
    __acggKind = "null";
  } else {
    __acggKind = typeof __acggValue;
  }
  return JSON.stringify({
    ok: true,
    kind: __acggKind,
    value: __acggValue === undefined ? null : __acggValue,
    error: ""
  });
} catch (__acggError) {
  const __acggMessage = __acggError && (__acggError.stack || __acggError.message)
    ? (__acggError.stack || __acggError.message)
    : String(__acggError);
  return JSON.stringify({
    ok: false,
    kind: "error",
    value: null,
    error: __acggMessage
  });
}
})();