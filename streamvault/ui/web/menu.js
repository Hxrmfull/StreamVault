/*
 * Меню действий: по кнопке «⋯» и по правому щелчку. Пункты — со значком и
 * подписью, по группам через черту: значок без подписи легко спутать
 * («открыть чат» и «скачать чат» выглядели одинаково). Вид — как у
 * выпадающих списков окна (dropdown.js), клавиатура — стрелки, Enter, Escape.
 */

import { h, icon } from "./common.js";

const CLOSE_MS = 140;
let current = null;            // открытое меню (оно всегда одно)

// at — кнопка (меню под ней) или {x, y} (правый щелчок); groups — [[{icon, label,
// onclick, danger, disabled, title}]], пустые группы и пункты null пропускаются
export function openActionMenu(at, groups, onClose) {
  closeActionMenu(true);
  const list = h("div", { class: "dropdown-menu action-menu", role: "menu", tabindex: "-1" });
  const rows = [];
  groups.map((group) => group.filter(Boolean)).filter((group) => group.length).forEach((group, index) => {
    if (index) list.append(h("div", { class: "action-menu-line", role: "separator" }));
    for (const entry of group) {
      // hint — строкой под названием: почему пункт серый, чтобы не искать подсказку
      const hint = h("span", { class: "action-menu-hint", text: entry.hint || "" });
      hint.hidden = !entry.hint;
      const row = h("div", { class: "dropdown-option" + (entry.danger ? " danger" : "") + (entry.disabled ? " disabled" : ""),
                             role: "menuitem", title: entry.title || "" },
        icon(entry.icon || "file"),
        h("span", { class: "dropdown-option-text" }, h("span", { text: entry.label }), hint));
      row._entry = entry;
      // доступность узнаётся не сразу (архив чатов — по сети): пока серый, потом как ответят
      if (entry.check) {
        row.classList.add("disabled");
        setHint(row, hint, entry.checking || "…");
        entry.check().then((state) => {
          row.classList.toggle("disabled", Boolean(state.disabled));
          setHint(row, hint, state.hint || "");
        }).catch((error) => setHint(row, hint, error.message || String(error)));
      }
      list.append(row);
      rows.push(row);
    }
  });
  const menu = { list, rows, active: -1, at, onClose };
  current = menu;
  (document.fullscreenElement || document.body).append(list);
  place(menu);
  requestAnimationFrame(() => list.classList.add("shown"));
  list.focus({ preventScroll: true });

  const choose = (row) => {
    if (!row || row.classList.contains("disabled")) return;
    closeActionMenu();
    row._entry.onclick();
  };
  list.addEventListener("click", (event) => {
    event.stopPropagation();
    choose(event.target.closest(".dropdown-option"));
  });
  list.addEventListener("mousemove", (event) => {
    const row = event.target.closest(".dropdown-option");
    if (row) setActive(menu, rows.indexOf(row));
  });
  list.addEventListener("keydown", (event) => {
    const enabled = rows.map((row, index) => (row.classList.contains("disabled") ? -1 : index)).filter((index) => index >= 0);
    const position = enabled.indexOf(menu.active);
    if (event.key === "ArrowDown") setActive(menu, enabled[(position + 1) % enabled.length]);
    else if (event.key === "ArrowUp") setActive(menu, enabled[(position - 1 + enabled.length) % enabled.length]);
    else if (event.key === "Home") setActive(menu, enabled[0]);
    else if (event.key === "End") setActive(menu, enabled[enabled.length - 1]);
    else if (event.key === "Enter" || event.key === " ") choose(rows[menu.active]);
    else if (event.key === "Escape" || event.key === "Tab") {
      closeActionMenu();
      if (at instanceof Element) at.focus({ preventScroll: true });
    } else return;
    event.preventDefault();
    event.stopPropagation();
  });
  return menu;
}

export function closeActionMenu(instant) {
  const menu = current;
  if (!menu) return;
  current = null;
  menu.list.classList.remove("shown");
  if (instant) menu.list.remove();
  else setTimeout(() => menu.list.remove(), CLOSE_MS);
  if (menu.onClose) menu.onClose();
}

function setHint(row, hint, text) {
  hint.textContent = text;
  hint.hidden = !text;
  row.title = text;
}

function setActive(menu, index) {
  menu.active = index === undefined ? -1 : index;
  menu.rows.forEach((row, position) => row.classList.toggle("active", position === menu.active));
  if (menu.rows[menu.active]) menu.rows[menu.active].scrollIntoView({ block: "nearest" });
}

function place(menu) {
  const { list, at } = menu;
  const gap = 4, margin = 8;
  const rect = at instanceof Element ? at.getBoundingClientRect()
    : { left: at.x, right: at.x, top: at.y, bottom: at.y };
  // высота с рамкой (offsetHeight): по scrollHeight меню выходило на 2 пикселя
  // ниже и показывало лишнюю полосу прокрутки
  const room = window.innerHeight - 2 * margin;
  const height = Math.min(list.offsetHeight, room);
  if (list.offsetHeight > room) list.style.maxHeight = room + "px";
  const width = list.offsetWidth;
  // у кнопки — правым краем к её правому краю; у щелчка — от точки щелчка
  const left = at instanceof Element ? rect.right - width : rect.left;
  list.style.left = Math.max(margin, Math.min(left, window.innerWidth - width - margin)) + "px";
  const below = window.innerHeight - rect.bottom - gap - margin;
  const up = height > below && rect.top > below;
  list.classList.toggle("up", up);
  list.style.top = Math.max(margin, up ? rect.top - gap - height : rect.bottom + gap) + "px";
}

// щелчок мимо, прокрутка, смена размера окна — меню закрывается
document.addEventListener("pointerdown", (event) => {
  if (!current || current.list.contains(event.target)) return;
  if (current.at instanceof Element && current.at.contains(event.target)) return;
  closeActionMenu();
}, true);
document.addEventListener("scroll", (event) => {
  if (current && !current.list.contains(event.target)) closeActionMenu();
}, true);
window.addEventListener("resize", () => closeActionMenu(true));
window.addEventListener("blur", () => closeActionMenu(true));
