(() => {
  const css = __ACGG_CSS__;
  try {
    if (window.__acggrScrollbarSheet) {
      window.__acggrScrollbarSheet.replaceSync(css);
      return;
    }
    const sheet = new CSSStyleSheet();
    sheet.replaceSync(css);
    window.__acggrScrollbarSheet = sheet;
    document.adoptedStyleSheets = [...document.adoptedStyleSheets, sheet];
  } catch (e) {}
})();
