// Page chrome: the light/dark theme and the header's pop-up menus.
// Copied from Pengumon (commit 14ab11d), owned by PenguWatch from here on.
//
// Theme: the operating system's choice applies until the person picks one with the header button; the pick is kept
// in this browser (localStorage `penguwatch.theme`) and set as <html data-theme>, which tokens.css reads. theme.js
// applies a kept pick before the page is painted, so it never flashes. The charts (Insights, System) are SVG styled
// by class, so they follow the tokens without being redrawn.
//
// Menus: a button with data-action="toggle-menu" data-arg="<menu id>" opens that .menu under itself; a click
// elsewhere, Escape or a choice closes it.
import { registerAction } from "./dispatch.js";

const THEME_KEY = "penguwatch.theme";
const dark = window.matchMedia("(prefers-color-scheme: dark)");

function storedTheme() {
  try {
    const t = localStorage.getItem(THEME_KEY);
    return t === "light" || t === "dark" ? t : "";
  } catch {
    return "";
  }
}

export const isDark = () => (document.documentElement.dataset.theme ? document.documentElement.dataset.theme === "dark" : dark.matches);

function themeButton() {
  const button = document.getElementById("btn-theme");
  if (button) {
    const next = isDark() ? "light" : "dark";
    button.setAttribute("aria-label", `Switch to the ${next} theme`);
    button.title = `Switch to the ${next} theme`;
    button.querySelector("use")?.setAttribute("href", isDark() ? "#i-sun" : "#i-moon");
  }
  const meta = document.querySelector('meta[name="theme-color"]');
  if (meta) meta.content = getComputedStyle(document.documentElement).getPropertyValue("--bg").trim() || meta.content;
}

function setTheme(theme) {
  if (theme) document.documentElement.dataset.theme = theme;
  else delete document.documentElement.dataset.theme;
  themeButton();
  document.dispatchEvent(new CustomEvent("themechange"));
}

export function toggleTheme() {
  const next = isDark() ? "light" : "dark";
  try {
    localStorage.setItem(THEME_KEY, next);
  } catch {
    // not kept; applies until the page is closed
  }
  setTheme(next);
}

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
  menu.hidden = false;
  button.setAttribute("aria-expanded", "true");
  openMenu = { menu, button };
  menu.querySelector("a, button")?.focus();
}

export function initChrome() {
  setTheme(storedTheme());
  dark.addEventListener("change", () => { if (!document.documentElement.dataset.theme) setTheme(""); });
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

registerAction("toggle-theme", () => toggleTheme());
registerAction("toggle-menu", (el) => toggleMenu(el));
