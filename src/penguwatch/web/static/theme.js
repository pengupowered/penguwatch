// Applies the theme picked with the header button (js/chrome.js keeps it) before the page is painted, so the page
// never flashes the other theme. A classic script in <head>, not a module: modules run only after parsing.
try {
  var pick = localStorage.getItem("penguwatch.theme");
  if (pick === "light" || pick === "dark") document.documentElement.dataset.theme = pick;
} catch (e) {
  // storage is off: the operating system's setting applies
}
