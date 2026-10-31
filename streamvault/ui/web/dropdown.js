/*
 * Выпадающие списки в стиле окна вместо системных.
 *
 * Системный <select> в WebView2 рисует свой список и стрелку по-своему:
 * не в тон окну, без анимации. Поэтому каждый <select>, появившийся на
 * странице, обёртывается: сам он остаётся (спрятанным) и по-прежнему
 * хранит значение и шлёт input и change, а видно кнопку и свой список.
 * Код страниц ничего об этом не знает: читает и пишет select.value,
 * меняет варианты и disabled как раньше — обёртка подстраивается.
 *
 * Клавиатура как у системного: Enter, пробел и стрелки открывают список,
 * стрелки, Home и End ходят по нему, буквы ищут вариант, Enter выбирает,
 * Escape и Tab закрывают.
 */

import { icon } from "./common.js";

const ENHANCED = new WeakMap();      // select → его обёртка
const VALUE = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "value");
const INDEX = Object.getOwnPropertyDescriptor(HTMLSelectElement.prototype, "selectedIndex");
const CLOSE_MS = 140;                // столько длится исчезание — потом список убирается
let openMenu = null;                 // открытый сейчас список (он всегда один)

/* Обёртывает все select внутри root и все, что появятся там потом. */
export function enhanceSelects(root) {
  root.querySelectorAll("select").forEach(enhance);
  new MutationObserver((records) => {
    for (const record of records) {
      for (const node of record.addedNodes) {
        if (node.nodeType !== 1) continue;
        if (node.tagName === "SELECT") enhance(node);
        else node.querySelectorAll("select").forEach(enhance);
      }
    }
  }).observe(root, { childList: true, subtree: true });
}

function enhance(select) {
  if (select.multiple || select.size > 1) return;
  const known = ENHANCED.get(select);
  if (known) {
    // страница переставила сам select — обёртка переезжает вместе с ним
    if (select.parentNode !== known.wrap) {
      select.parentNode.insertBefore(known.wrap, select);
      known.wrap.prepend(select);
    }
    return;
  }
  const value = h("span", "dropdown-value");
  const button = document.createElement("button");
  button.type = "button";
  button.className = "dropdown-button";
  button.setAttribute("aria-haspopup", "listbox");
  button.setAttribute("aria-expanded", "false");
  const chevron = icon("chevron");
  chevron.classList.add("dropdown-chevron");
  button.append(value, chevron);
  const wrap = h("span", "dropdown");
  for (const name of select.classList) wrap.classList.add(name);
  select.parentNode.insertBefore(wrap, select);
  wrap.append(select, button);
  select.classList.add("dropdown-native");
  select.tabIndex = -1;
  select.setAttribute("aria-hidden", "true");

  const self = { select, wrap, button, value };
  ENHANCED.set(select, self);
  // запись select.value и selectedIndex из кода — сразу видна на кнопке
  Object.defineProperty(select, "value", {
    configurable: true,
    get() { return VALUE.get.call(select); },
    set(next) { VALUE.set.call(select, next); refresh(self); },
  });
  Object.defineProperty(select, "selectedIndex", {
    configurable: true,
    get() { return INDEX.get.call(select); },
    set(next) { INDEX.set.call(select, next); refresh(self); },
  });
  // варианты заменили, disabled или подсказку поменяли
  new MutationObserver(() => refresh(self)).observe(select, {
    childList: true, subtree: true, characterData: true, attributes: true,
    attributeFilter: ["disabled", "title", "hidden", "selected", "label"],
  });
  select.addEventListener("change", () => refresh(self));

  button.addEventListener("click", () => (openMenu && openMenu.self === self ? closeMenu() : openList(self)));
  button.addEventListener("keydown", (event) => {
    if (["ArrowDown", "ArrowUp", "Enter", " "].includes(event.key)) {
      event.preventDefault();
      openList(self);
    }
  });
  refresh(self);
}

function h(tag, className) {
  const el = document.createElement(tag);
  el.className = className;
  return el;
}

function options(select) {
  return [...select.options].filter((option) => !option.hidden);
}

function refresh(self) {
  const { select, button, value, wrap } = self;
  const current = select.options[INDEX.get.call(select)];
  // все варианты в одной клетке сетки: кнопка шириной с самый длинный, как у системного
  const labels = options(select).map((option) => {
    const label = h("span", option === current ? "dropdown-label current" : "dropdown-label");
    label.textContent = option.label || option.textContent;
    return label;
  });
  if (!current) labels.push(Object.assign(h("span", "dropdown-label current"), { textContent: " " }));
  value.replaceChildren(...labels);
  button.disabled = select.disabled;
  wrap.classList.toggle("disabled", select.disabled);
  wrap.hidden = select.hidden;
  if (select.title) button.title = select.title;
  else button.removeAttribute("title");
  const name = select.getAttribute("aria-label") || select.title;
  if (name) button.setAttribute("aria-label", name + ": " + (current ? current.textContent : ""));
  if (openMenu && openMenu.self === self) fillList(openMenu);
}

/* ------------------------------------------------------------------ список */

function openList(self) {
  if (self.select.disabled) return;
  closeMenu(true);
  const list = h("div", "dropdown-menu");
  list.setAttribute("role", "listbox");
  list.tabIndex = -1;
  const menu = { self, list, active: -1, typed: "", typedAt: 0 };
  openMenu = menu;
  fillList(menu);
  // список — поверх всего, а не внутри карточки или окна с прокруткой
  (document.fullscreenElement || document.body).append(list);
  place(menu);
  self.button.setAttribute("aria-expanded", "true");
  self.wrap.classList.add("open");
  requestAnimationFrame(() => list.classList.add("shown"));
  list.focus({ preventScroll: true });
  scrollToActive(menu);

  list.addEventListener("keydown", (event) => onKey(menu, event));
  list.addEventListener("mousemove", (event) => {
    const row = event.target.closest(".dropdown-option");
    if (row && !row.classList.contains("disabled")) setActive(menu, Number(row.dataset.index), false);
  });
  list.addEventListener("click", (event) => {
    const row = event.target.closest(".dropdown-option");
    if (row && !row.classList.contains("disabled")) choose(menu, Number(row.dataset.index));
  });
}

function fillList(menu) {
  const { select } = menu.self;
  const selected = INDEX.get.call(select);
  const rows = [];
  [...select.options].forEach((option, index) => {
    if (option.hidden) return;
    const row = h("div", "dropdown-option");
    row.dataset.index = index;
    row.setAttribute("role", "option");
    row.setAttribute("aria-selected", String(index === selected));
    if (index === selected) row.classList.add("selected");
    if (option.disabled) row.classList.add("disabled");
    const text = h("span", "dropdown-option-text");
    text.textContent = option.label || option.textContent;
    const mark = icon("check");
    mark.classList.add("dropdown-check");
    row.append(text, mark);
    rows.push(row);
  });
  menu.list.replaceChildren(...rows);
  if (menu.active < 0) menu.active = selected;
  setActive(menu, menu.active, false);
}

function place(menu) {
  const rect = menu.self.button.getBoundingClientRect();
  const list = menu.list;
  const gap = 4, margin = 8;
  list.style.minWidth = rect.width + "px";
  list.style.maxHeight = "";
  const below = window.innerHeight - rect.bottom - gap - margin;
  const above = rect.top - gap - margin;
  const natural = Math.min(list.scrollHeight, 320);
  // снизу не помещается, а сверху места больше — открываем вверх
  const up = natural > below && above > below;
  list.classList.toggle("up", up);
  list.style.maxHeight = Math.max(120, Math.min(320, up ? above : below)) + "px";
  const width = list.offsetWidth;
  list.style.left = Math.max(margin, Math.min(rect.left, window.innerWidth - width - margin)) + "px";
  if (up) {
    list.style.top = "";
    list.style.bottom = (window.innerHeight - rect.top + gap) + "px";
  } else {
    list.style.bottom = "";
    list.style.top = (rect.bottom + gap) + "px";
  }
}

function closeMenu(instant) {
  const menu = openMenu;
  if (!menu) return;
  openMenu = null;
  menu.self.button.setAttribute("aria-expanded", "false");
  menu.self.wrap.classList.remove("open");
  const list = menu.list;
  list.classList.remove("shown");
  if (instant) list.remove();
  else setTimeout(() => list.remove(), CLOSE_MS);
}

function rows(menu) {
  return [...menu.list.querySelectorAll(".dropdown-option")];
}

function setActive(menu, index, scroll) {
  menu.active = index;
  for (const row of rows(menu)) row.classList.toggle("active", Number(row.dataset.index) === index);
  if (scroll) scrollToActive(menu);
}

function scrollToActive(menu) {
  const row = menu.list.querySelector(".dropdown-option.active");
  if (row) row.scrollIntoView({ block: "nearest" });
}

function choose(menu, index) {
  const { select, button } = menu.self;
  closeMenu();
  button.focus({ preventScroll: true });
  if (index === INDEX.get.call(select)) return;
  INDEX.set.call(select, index);
  refresh(menu.self);
  // как у системного: сначала input, потом change
  select.dispatchEvent(new Event("input", { bubbles: true }));
  select.dispatchEvent(new Event("change", { bubbles: true }));
}

function onKey(menu, event) {
  const enabled = rows(menu).filter((row) => !row.classList.contains("disabled")).map((row) => Number(row.dataset.index));
  const position = enabled.indexOf(menu.active);
  const step = (delta) => {
    const next = enabled[Math.max(0, Math.min(enabled.length - 1, (position < 0 ? 0 : position + delta)))];
    if (next !== undefined) setActive(menu, next, true);
  };
  switch (event.key) {
    case "ArrowDown": step(1); break;
    case "ArrowUp": step(-1); break;
    case "PageDown": step(8); break;
    case "PageUp": step(-8); break;
    case "Home": if (enabled.length) setActive(menu, enabled[0], true); break;
    case "End": if (enabled.length) setActive(menu, enabled[enabled.length - 1], true); break;
    case "Enter":
    case " ":
      if (menu.active >= 0) choose(menu, menu.active);
      break;
    case "Escape":
      closeMenu();
      menu.self.button.focus({ preventScroll: true });
      break;
    case "Tab":
      closeMenu();
      menu.self.button.focus({ preventScroll: true });
      return;          // фокус уходит дальше сам
    default:
      if (event.key.length === 1 && !event.ctrlKey && !event.metaKey && !event.altKey) typeAhead(menu, event.key, enabled);
      return;
  }
  event.preventDefault();
  event.stopPropagation();
}

function typeAhead(menu, key, enabled) {
  const now = Date.now();
  menu.typed = (now - menu.typedAt > 700 ? "" : menu.typed) + key.toLowerCase();
  menu.typedAt = now;
  const { select } = menu.self;
  const found = enabled.find((index) => select.options[index].textContent.trim().toLowerCase().startsWith(menu.typed));
  if (found !== undefined) setActive(menu, found, true);
}

// щелчок мимо, прокрутка страницы, смена размера окна — список закрывается
document.addEventListener("pointerdown", (event) => {
  if (!openMenu) return;
  if (openMenu.list.contains(event.target) || openMenu.self.button.contains(event.target)) return;
  closeMenu();
}, true);
document.addEventListener("scroll", (event) => {
  if (openMenu && !openMenu.list.contains(event.target)) closeMenu();
}, true);
window.addEventListener("resize", () => closeMenu(true));
window.addEventListener("blur", () => closeMenu(true));
document.addEventListener("fullscreenchange", () => closeMenu(true));
