// Runs before first paint so a saved theme doesn't flash.
(function () {
  try {
    var saved = localStorage.getItem('rcc-theme');
    if (saved === 'light' || saved === 'dark') document.documentElement.dataset.theme = saved;
  } catch (error) { /* Storage can be blocked. The system theme still applies. */ }
})();
