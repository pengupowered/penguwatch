// Page chrome: the header's pop-up menus, and the start of the themes (js/themes.js).
//
// Theme: the header's theme menu (js/themes.js) picks a theme and light or dark; the pick is kept in this browser and set
// as <html data-look> and <html data-theme>, which tokens.css and the theme's stylesheet read. theme.js applies a kept
// pick before the page is painted, so it never flashes. The charts (Insights, System) are SVG styled by class, so they
// follow the tokens without being redrawn.
//
// Menus: a button with data-action="toggle-menu" data-arg="<menu id>" opens that .menu under itself; a click
// elsewhere, Escape or a choice closes it.
import { registerAction } from "./dispatch.js";
import { fillThemeMenu, initThemes } from "./themes.js";

// ---------- menus ----------
let openMenu = null; // { menu, button }

export function closeMenus() {
  if (!openMenu) return;
  openMenu.menu.hidden = true;
  openMenu.button.setAttribute("aria-expanded", "false");
  openMenu = null;
}

function toggleMenu(button) {
  const menu = document.getElementById(button.dataset.arg);
  if (!menu) return;
  const wasOpen = openMenu && openMenu.menu === menu;
  closeMenus();
  if (wasOpen) return;
  if (menu.id === "theme-menu") fillThemeMenu(menu); // its checked items show the choice as it is now
  menu.hidden = false;
  button.setAttribute("aria-expanded", "true");
  openMenu = { menu, button };
  menu.querySelector("a, button")?.focus();
}

export function initChrome() {
  initThemes();
  document.addEventListener("click", (ev) => {
    if (!openMenu) return;
    if (openMenu.button.contains(ev.target)) return;
    if (openMenu.menu.contains(ev.target) && !ev.target.closest("a, button")) return;
    closeMenus();
  });
  document.addEventListener("keydown", (ev) => {
    if (ev.key !== "Escape" || !openMenu) return;
    const { button } = openMenu;
    closeMenus();
    button.focus();
  });
}

registerAction("toggle-menu", (el) => toggleMenu(el));
