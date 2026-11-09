/*
 * StreamVault, окно программы: чат записи без видео — читать прямо в окне,
 * а не в браузере через .chat.html.
 *
 * Сверху — график активности: где чат писал больше всего; щелчок по нему —
 * к этому месту, протяжка с зажатой кнопкой листает ленту следом. Лента
 * сообщений со смайлами и значками догружается при прокрутке в обе стороны.
 * Поиск по словам или нику. Справа — панель «Зрители»: щелчок по нику в ленте
 * (или ник в поле) добавляет зрителя, и там видны сообщения всех выбранных
 * вместе; щелчок по найденному или по сообщению зрителя — к нему в ленте.
 */

import { bridge, button, channelName, displayTitle, failed, fmt, h, icon, iconButton, openModal, platformName, setText,
  t, tp } from "./common.js";
import { openSummary } from "./summary.js";

const CHUNK = 600;            // секунд чата за один запрос
const KEEP_CHUNKS = 8;        // столько кусков в ленте, дальние убираются
const EDGE = 800;             // пикселей до края ленты — пора догружать
const MIN_LINES = 80;         // после перехода — хотя бы столько строк, чтобы было что листать
const BEFORE = 20;            // переход к месту: показать и пару десятков секунд до него
// протяжка по графику: лента — маленькими кусками, только на экран; остальное — после
const SCRUB_CHUNK = 90;
const SCRUB_LINES = 40;
const SVG = "http://www.w3.org/2000/svg";
const USERS_LIMIT = 20;       // зрителей выбрать сразу (ui/api.py: USERS_LIMIT)

// «1:23:45», «83:20», «90» → секунды; не время — null
function parseTime(text) {
  const parts = String(text).trim().split(":");
  if (!parts[0] || parts.length > 3 || parts.some((part) => !/^\d+(\.\d+)?$/.test(part))) return null;
  return parts.reduce((total, part) => total * 60 + Number(part), 0);
}

// item — запись библиотеки (или чат без видео); options.start — открыть на этом месте,
// options.mark — сообщение, которое подсветить; options.user — сразу сообщения этого зрителя
export function openChatView(item, options) {
  options = options || {};
  let chat = null, closed = false;
  const modal = openModal({ kind: "chat-view", onClose: () => {
    closed = true;
    document.removeEventListener("keydown", onKey, true);
  } });

  // части эфира с чатом — переключатель, если их больше одной
  const parts = (item.parts || [item]).filter((part) => part.chat && part.chat.json);
  let current = parts.find((part) => part.path === options.path) || parts[0] || item;

  const meta = h("div", { class: "muted" });
  const source = h("div", { class: "chatview-source" });
  source.hidden = true;
  // поиск — полем со значком, как у остального окна; Ctrl+F ставит сюда курсор
  const searchInput = h("input", { type: "search", spellcheck: "false", placeholder: t("Слово, фраза или смайл") });
  const searchBox = h("label", { class: "chatview-searchbox", title: t("Поиск по чату (Ctrl+F)") },
    icon("search"), searchInput, h("kbd", { text: "Ctrl+F" }));
  const by = h("select", { title: t("Где искать") },
    h("option", { value: "text", text: t("в сообщениях") }), h("option", { value: "name", text: t("по нику") }));
  // переход ко времени: маска «00:00:00» уже стоит, цифры заполняют её слева направо,
  // каретка — сразу за набранным. Поверх поля — та же строка: набранное ярко, остальное бледно
  const timeInput = h("input", { type: "text", inputmode: "numeric", spellcheck: "false", autocomplete: "off",
                                 "aria-label": t("Время") });
  const timeShown = h("div", { class: "chatview-time-shown", "aria-hidden": "true" });
  const timeField = h("label", { class: "chatview-time" }, timeShown, timeInput);
  const timeGo = iconButton("play", t("Перейти"), () => goToTime());
  const timeBox = h("div", { class: "chatview-timebox", title: t("Перейти к месту чата: время и Enter") },
    h("span", { class: "muted", text: t("Перейти к") }), timeField, timeGo);
  const partSelect = h("select", { title: t("Часть эфира") }, parts.map((part, index) =>
    h("option", { value: String(index), text: t("часть {part} из {count}", { part: index + 1, count: parts.length }) })));
  partSelect.hidden = parts.length < 2;
  const activity = h("div", { class: "chatview-activity",
    title: t("Активность чата: щелчок переносит к этому месту, ведение с зажатой кнопкой листает чат") });
  const bars = document.createElementNS(SVG, "svg");
  bars.setAttribute("preserveAspectRatio", "none");
  bars.classList.add("chatview-bars");
  const cursor = h("div", { class: "chatview-cursor" });
  const hoverTime = h("div", { class: "chatview-hover" });
  hoverTime.hidden = true;
  const peaks = h("div", { class: "chatview-peaks" });
  // пропуски чата (разрывы связи при записи, подозрительная тишина) — штриховкой
  const gapMarks = h("div", { class: "chatview-gaps" });
  activity.append(gapMarks, bars, peaks, cursor, hoverTime);
  const status = h("span", { class: "chatview-status" });
  // выбранные зрители — справа от ленты: поле для ника, плашки и их сообщения вместе;
  // ник добавляется щелчком в ленте или вписывается в поле; щелчок по сообщению — к нему в ленте
  const users = [];
  const chips = h("div", { class: "chatview-chips" });
  const userInput = h("input", { type: "text", class: "chatview-user-input", spellcheck: "false", autocomplete: "off",
                                 placeholder: t("Добавить ник…"), "aria-label": t("Добавить ник") });
  // подсказки ников: точный ник можно не знать — хватит части, пустое поле — самые активные
  const suggest = h("div", { class: "chatview-suggest", role: "listbox" });
  suggest.hidden = true;
  const userBox = h("div", { class: "chatview-user-box" }, userInput, suggest);
  const allChat = button(t("Весь чат"), { kind: "ghost", icon: "chat", onclick: () => {
    searchInput.value = "";
    ++searchGeneration;
    showFeed();
    showPosition();
  } });
  const userStatus = h("div", { class: "chatview-side-status" });
  const userList = h("div", { class: "chatview-list chatview-side-list" });
  const side = h("aside", { class: "chatview-side" },
    h("div", { class: "chatview-side-top" }, h("div", { class: "chatview-side-title", text: t("Зрители") }),
      userBox, chips, userStatus),
    userList);
  const position = h("span", { class: "chatview-position" });
  const list = h("div", { class: "chatview-list" }, h("div", { class: "spinner" }));
  const found = h("div", { class: "chatview-list" });
  found.hidden = true;
  const feed = h("div", { class: "chatview-feed" },
    h("div", { class: "chatview-bar" }, status, allChat, h("span", { class: "spacer" }), position),
    list, found);

  const tools = h("div", { class: "toolbar chatview-tools" }, searchBox, by, timeBox, partSelect);
  const main = h("div", { class: "chatview-main" }, feed, side);
  // чат не открылся (файла нет, испорчен) — вместо ленты одна надпись по центру, как в пустых вкладках
  const problem = h("div", { class: "empty chatview-problem" });
  problem.hidden = true;
  let head = null;

  modal.body.append(
    h("h2", { text: displayTitle(item.title) || item.title }),
    // сведения о чате — и рядом то, что открывает его иначе: сводка, браузер
    head = h("div", { class: "chatview-head" }, h("div", null, meta, source), h("span", { class: "spacer" }),
      button(item.video_kind === "chat" ? t("Сводка чата") : t("Сводка эфира"), { icon: "stats", kind: "ghost",
        onclick: () => openSummary(item) }),
      current.chat && current.chat.html ? button(t("Открыть в браузере"), { icon: "external", kind: "ghost",
        onclick: () => bridge.call("open_file", current.chat.html).catch(failed) }) : null),
    tools, activity, main, problem);
  list.tabIndex = -1;                 // стрелки и PageDown листают ленту

  // ---------------------------------------------------------------- лента

  let chunks = [];            // [{from, to, nodes}] по порядку времени
  let loading = false, generation = 0;

  function imageSrc(index) {
    const url = chat.images[index];
    return typeof url === "string" && /^(https:\/\/|http:\/\/127\.0\.0\.1:)/i.test(url) ? url : "";
  }

  function line(message, onclick) {
    const [offset, name, color, badges, partsOfMessage] = message;
    const row = h("div", { class: "chat-line" });
    row.dataset.offset = offset;
    row.dataset.name = name;
    row.append(h("span", { class: "chat-time", text: fmt.duration(offset) }));
    for (const index of badges) {
      const src = imageSrc(index);
      if (src) row.append(h("img", { class: "chat-badge", src: src, alt: "" }));
    }
    const nameEl = h("button", { type: "button", class: "chat-name", text: name,
                                 title: t("Все сообщения {name}", { name: name }) });
    if (/^#[0-9a-f]{6}$/i.test(color)) nameEl.style.color = color;
    nameEl.addEventListener("click", (event) => {
      event.stopPropagation();
      addUser(name);
    });
    row.append(nameEl, ": ");
    for (const part of partsOfMessage) {
      if (typeof part === "string") {
        row.append(part);
        continue;
      }
      const src = imageSrc(part[0]);
      row.append(src ? h("img", { class: "chat-emote", src: src, alt: part[1], title: part[1] }) : part[1]);
    }
    if (onclick) {
      row.classList.add("found");
      row.title = t("Перейти к этому месту в чате");
      row.addEventListener("click", () => onclick(message));
    }
    return row;
  }

  function fetch(from, to) {
    return bridge.call("chat_replay", chat.id, from, to);
  }

  // перейти к месту: лента заново, от чуть раньше него; quiet — без крутилки
  // (протяжка по графику: старое место видно, пока не пришло новое)
  async function jumpTo(seconds, mark, quiet, light) {
    const mine = ++generation;
    seconds = Math.max(0, Math.min(seconds, chat.length || seconds));
    if (mode !== "feed") showFeed();
    if (!quiet) list.replaceChildren(h("div", { class: "spinner" }));
    chunks = [];
    let from = Math.max(0, seconds - BEFORE);
    let to = from;
    const nodes = [];
    loading = true;
    try {
      // реже пишут — берём кусками, пока не наберётся, что листать
      const step = light ? SCRUB_CHUNK : CHUNK;
      do {
        let messages = await fetch(to, to + step);
        if (mine !== generation || closed) return;
        to += step;
        if (light && nodes.length + messages.length > SCRUB_LINES) {
          // при протяжке — только строки на экран: лента дальше догрузится с этого места
          const cutoff = messages[SCRUB_LINES - nodes.length][0];
          // много сообщений в одну секунду — не резать посреди неё, иначе кусок вышел бы пустым
          if (cutoff > messages[0][0]) {
            messages = messages.filter((message) => message[0] < cutoff);
            to = cutoff;
          }
        }
        nodes.push(...messages.map((message) => line(message)));
      } while (nodes.length < (light ? SCRUB_LINES : MIN_LINES) && to < (chat.length || 0));
    } catch (error) {
      if (mine === generation) list.replaceChildren(h("div", { class: "warn-text", text: error.message || String(error) }));
      return;
    } finally {
      if (mine === generation) loading = false;
    }
    chunks = [{ from: from, to: to, nodes: nodes }];
    list.replaceChildren(...nodes);
    if (!nodes.length) list.append(h("div", { class: "chatview-empty", text: t("Здесь чат молчал.") }));
    // строка места — вверху ленты; сообщение из поиска — подсвечено
    const target = nodes.find((node) => Number(node.dataset.offset) >= seconds) || nodes[nodes.length - 1];
    if (target) {
      list.scrollTop = Math.max(0, target.offsetTop - list.offsetTop - 40);
      if (mark) {
        const exact = nodes.find((node) => Number(node.dataset.offset) === mark[0] && node.dataset.name === mark[1]);
        (exact || target).classList.add("chatview-mark");
      }
    }
    // сверху пусто — догрузить и туда, чтобы можно было листать назад (при протяжке — после неё)
    if (!light && from > 0 && list.scrollTop < EDGE) loadBefore();
    showPosition();
  }

  async function loadAfter() {
    const last = chunks[chunks.length - 1];
    if (loading || !last || last.to >= (chat.length || 0)) return;
    loading = true;
    const mine = generation;
    try {
      const messages = await fetch(last.to, last.to + CHUNK);
      if (mine !== generation || closed) return;
      const nodes = messages.map((message) => line(message));
      list.append(...nodes);
      chunks.push({ from: last.to, to: last.to + CHUNK, nodes: nodes });
      // дальние куски сверху — прочь; высота ленты уменьшается, место на экране — нет
      while (chunks.length > KEEP_CHUNKS) {
        const dropped = chunks.shift();
        const height = list.scrollHeight;
        dropped.nodes.forEach((node) => node.remove());
        list.scrollTop -= height - list.scrollHeight;
      }
    } catch (error) {
      failed(error);
    } finally {
      if (mine === generation) loading = false;
    }
  }

  async function loadBefore() {
    const first = chunks[0];
    if (loading || !first || first.from <= 0) return;
    loading = true;
    const mine = generation;
    const from = Math.max(0, first.from - CHUNK);
    try {
      const messages = await fetch(from, first.from);
      if (mine !== generation || closed) return;
      const nodes = messages.map((message) => line(message));
      // добавленное сверху не должно сдвигать то, что читают
      const height = list.scrollHeight;
      list.prepend(...nodes);
      list.scrollTop += list.scrollHeight - height;
      chunks.unshift({ from: from, to: first.from, nodes: nodes });
      while (chunks.length > KEEP_CHUNKS) chunks.pop().nodes.forEach((node) => node.remove());
    } catch (error) {
      failed(error);
    } finally {
      if (mine === generation) loading = false;
    }
  }

  // где сейчас лента: время первой видной строки — подписью и чертой на графике
  let positionFrame = 0;
  function showPosition() {
    cancelAnimationFrame(positionFrame);
    positionFrame = requestAnimationFrame(() => {
      const top = list.scrollTop + list.offsetTop;
      const nodes = list.children;
      let low = 0, high = nodes.length - 1;
      while (low < high) {
        const middle = (low + high) >> 1;
        if (nodes[middle].offsetTop + nodes[middle].offsetHeight <= top) low = middle + 1; else high = middle;
      }
      const node = nodes[low];
      const seconds = node && node.dataset.offset !== undefined ? Number(node.dataset.offset) : null;
      setText(position, seconds === null ? "" : fmt.duration(seconds));
      if (dragging) return;              // при протяжке черта — под мышью, а не у отстающей ленты
      cursor.hidden = seconds === null || !chat.length;
      if (seconds !== null && chat.length) cursor.style.left = Math.min(100, seconds / chat.length * 100) + "%";
    });
  }

  list.addEventListener("scroll", () => {
    showPosition();
    if (list.scrollHeight - list.scrollTop - list.clientHeight < EDGE) loadAfter();
    else if (list.scrollTop < EDGE) loadBefore();
  });

  // ---------------------------------------------------------------- поиск и зритель

  let mode = "feed";            // feed — вся лента, found — найденное поиском

  function showFeed() {
    mode = "feed";
    found.hidden = true;
    list.hidden = false;
    allChat.hidden = true;
    setText(status, fmt.counted(chat.count, t("сообщение|сообщения|сообщений")));
  }

  function showFound(messages, text) {
    mode = "found";
    allChat.hidden = false;
    list.hidden = true;
    found.hidden = false;
    setText(status, text);
    found.replaceChildren(...messages.map((message) => line(message, (picked) => {
      searchInput.value = "";
      jumpTo(picked[0], picked);
    })));
    if (!messages.length) found.append(h("div", { class: "chatview-empty", text: t("Ничего не нашлось.") }));
    found.scrollTop = 0;
  }

  let searchTimer = null, searchGeneration = 0;
  function search() {
    const text = searchInput.value.trim();
    const mine = ++searchGeneration;
    if (!text) {
      showFeed();
      return;
    }
    bridge.call("chat_search", chat.id, text, by.value).then((messages) => {
      if (mine !== searchGeneration || closed) return;
      // окно отдаёт первые SEARCH_LIMIT — об этом честно
      showFound(messages, messages.length >= 200 ? t("Найдено больше {count} — показаны первые", { count: 200 })
                                                  : t("Найдено: {count}", { count: messages.length }));
    }).catch(failed);
  }
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(search, 300);
  });
  by.addEventListener("change", search);
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && searchInput.value) {
      event.stopPropagation();
      searchInput.value = "";
      search();
    }
  });

  // плашки выбранных зрителей с ×
  function drawUsers() {
    chips.replaceChildren(...users.map((name) => {
      const remove = h("button", { type: "button", class: "chatview-chip-x", title: t("Убрать {name}", { name: name }),
                                   text: "×" });
      remove.addEventListener("click", () => {
        users.splice(users.indexOf(name), 1);
        showUsers();
      });
      return h("span", { class: "chatview-chip" }, h("span", { text: name }), remove);
    }));
    chips.hidden = !users.length;
  }

  function addUser(name) {
    name = String(name).trim().replace(/^@/, "");
    if (!name || users.some((known) => known.toLowerCase() === name.toLowerCase())) return;
    if (users.length >= USERS_LIMIT) {
      failed(new Error(t("Зрителей не больше {count}", { count: USERS_LIMIT })));
      return;
    }
    users.push(name);
    showUsers();
  }

  // сообщения всех выбранных зрителей вместе, по времени — в панели справа;
  // щелчок по сообщению — к нему в ленте, лента и панель видны вместе
  let usersGeneration = 0;
  function showUsers() {
    drawUsers();
    const mine = ++usersGeneration;
    if (!users.length) {
      setText(userStatus, "");
      userList.replaceChildren(h("div", { class: "chatview-empty",
        text: t("Щелчок по нику в чате добавит зрителя сюда, ник можно и вписать в поле выше.") }));
      return;
    }
    bridge.call("chat_search", chat.id, users.slice(), "users").then((messages) => {
      if (closed || mine !== usersGeneration) return;
      setText(userStatus, users.length === 1
        ? t("Сообщения {name}: {count}", { name: users[0], count: fmt.count(messages.length) })
        : t("Сообщения выбранных зрителей ({users}): {count}", { users: users.length, count: fmt.count(messages.length) }));
      userList.replaceChildren(...messages.map((message) => line(message, (picked) => {
        searchInput.value = "";
        ++searchGeneration;
        jumpTo(picked[0], picked);
      })));
      if (!messages.length) userList.append(h("div", { class: "chatview-empty", text: t("Ничего не нашлось.") }));
      userList.scrollTop = 0;
    }).catch(failed);
  }
  let suggestions = [], picked = -1, suggestTimer = null, suggestGeneration = 0;

  function showSuggestions() {
    const mine = ++suggestGeneration;
    // несколько ников через запятую — подсказка к последнему
    const typed = userInput.value.split(/[\s,]+/).pop();
    bridge.call("chat_names", chat.id, typed).then((names) => {
      if (closed || mine !== suggestGeneration || document.activeElement !== userInput) return;
      const chosen = new Set(users.map((name) => name.toLowerCase()));
      suggestions = names.filter(([name]) => !chosen.has(name.toLowerCase()));
      picked = suggestions.length && typed ? 0 : -1;
      drawSuggestions(typed);
    }).catch(() => { suggest.hidden = true; });
  }

  function drawSuggestions(typed) {
    suggest.replaceChildren(...suggestions.map(([name, count], index) => {
      const row = h("button", { type: "button", role: "option", class: "chatview-suggest-row" + (index === picked ? " active" : "") },
        h("span", { class: "chatview-suggest-name" }, ...markMatch(name, typed)),
        h("span", { class: "muted", text: fmt.count(count) }));
      // mousedown, а не click: иначе поле теряет фокус раньше и список прячется
      row.addEventListener("mousedown", (event) => {
        event.preventDefault();
        takeSuggestion(name);
      });
      return row;
    }));
    if (!suggestions.length && typed) suggest.append(h("div", { class: "chatview-suggest-empty", text: t("Таких ников в чате нет") }));
    suggest.hidden = !suggestions.length && !typed;
  }

  // совпавшая часть ника — выделена
  function markMatch(name, typed) {
    const at = typed ? name.toLowerCase().indexOf(typed.toLowerCase()) : -1;
    if (at < 0) return [name];
    return [name.slice(0, at), h("b", { text: name.slice(at, at + typed.length) }), name.slice(at + typed.length)];
  }

  function takeSuggestion(name) {
    const before = userInput.value.split(/[\s,]+/).slice(0, -1).filter(Boolean);
    for (const earlier of before) addUser(earlier);
    addUser(name);
    userInput.value = "";
    suggest.hidden = true;
    userInput.focus();
    showSuggestions();
  }

  userInput.addEventListener("focus", showSuggestions);
  userInput.addEventListener("input", () => {
    clearTimeout(suggestTimer);
    suggestTimer = setTimeout(showSuggestions, 120);
  });
  userInput.addEventListener("blur", () => { setTimeout(() => { suggest.hidden = true; }, 100); });
  userInput.addEventListener("keydown", (event) => {
    if ((event.key === "ArrowDown" || event.key === "ArrowUp") && suggestions.length) {
      event.preventDefault();
      picked = (picked + (event.key === "ArrowDown" ? 1 : -1) + suggestions.length) % suggestions.length;
      drawSuggestions(userInput.value.split(/[\s,]+/).pop());
      return;
    }
    if (event.key === "Escape" && !suggest.hidden) {
      event.stopPropagation();
      suggest.hidden = true;
      return;
    }
    if (event.key !== "Enter") return;
    event.preventDefault();
    // выбрана подсказка — она; иначе как вписано (можно несколько: через запятую или пробел)
    if (picked >= 0 && suggestions[picked]) {
      takeSuggestion(suggestions[picked][0]);
      return;
    }
    for (const name of userInput.value.split(/[\s,]+/).filter(Boolean)) addUser(name);
    userInput.value = "";
    suggest.hidden = true;
  });

  const timeDigits = ["0", "0", "0", "0", "0", "0"];
  let timeTyped = 0;                   // сколько цифр маски набрано

  // где каретка после n набранных цифр: двоеточия перешагиваются
  const caretAt = (typed) => typed + Math.min(2, Math.floor(typed / 2));

  function drawTime() {
    const text = timeDigits.slice(0, 2).join("") + ":" + timeDigits.slice(2, 4).join("") + ":"
      + timeDigits.slice(4).join("");
    timeInput.value = text;
    const at = timeTyped ? caretAt(timeTyped) : 0;
    timeShown.replaceChildren(h("span", { class: "typed", text: text.slice(0, at) }), text.slice(at));
    if (document.activeElement === timeInput) timeInput.setSelectionRange(at, at);
  }

  function typeDigit(digit) {
    if (timeTyped >= 6) return;
    // десятки минут и секунд — не больше 5: «7» там — это «07»
    if (timeTyped % 2 === 0 && timeTyped > 0 && digit > "5") {
      timeDigits[timeTyped] = "0";
      timeTyped++;
    }
    timeDigits[timeTyped] = digit;
    timeTyped++;
  }

  function clearTime() {
    timeDigits.fill("0");
    timeTyped = 0;
  }

  function goToTime() {
    if (!timeTyped) {
      timeInput.focus();               // пусто — не ошибка: просто курсор в поле
      return;
    }
    const [hours, minutes, seconds] = [0, 2, 4].map((at) => Number(timeDigits[at] + timeDigits[at + 1]));
    if (minutes > 59 || seconds > 59) {
      timeField.classList.add("invalid");
      timeInput.focus();
      return;
    }
    timeField.classList.remove("invalid");
    searchInput.value = "";
    ++searchGeneration;
    jumpTo(hours * 3600 + minutes * 60 + seconds);
  }

  // время в поле — из любой записи: «1:23:45», «83:20», «90»
  function fillTime(text) {
    let total = parseTime(text);
    if (total === null) return false;
    total = Math.min(Math.floor(total), 99 * 3600 + 3599);
    const values = [Math.floor(total / 3600), Math.floor(total / 60) % 60, total % 60];
    values.map((value) => String(value).padStart(2, "0")).join("").split("")
      .forEach((digit, index) => { timeDigits[index] = digit; });
    timeTyped = 6;
    return true;
  }

  timeInput.addEventListener("keydown", (event) => {
    if (event.ctrlKey || event.metaKey || event.altKey) {
      // Ctrl+A, Ctrl+C, Ctrl+V — как обычно; остальное поле не трогает
      return;
    }
    if (event.key === "Tab") return;
    event.preventDefault();
    timeField.classList.remove("invalid");
    const all = timeInput.selectionStart === 0 && timeInput.selectionEnd === timeInput.value.length && timeTyped;
    if (event.key === "Enter") {
      goToTime();
      return;
    }
    if (event.key === "Escape") {
      if (timeTyped) {
        event.stopPropagation();       // сначала очистить поле, окно — следующим Escape
        clearTime();
      } else {
        timeInput.blur();
      }
    } else if (/^[0-9]$/.test(event.key)) {
      if (all) clearTime();            // всё выделено — набранное заменяется
      typeDigit(event.key);
    } else if (event.key === "Backspace" || event.key === "Delete") {
      if (all) clearTime();
      else if (timeTyped) timeDigits[--timeTyped] = "0";
    } else if ((event.key === ":" || event.key === " " || event.key === ".") && timeTyped % 2 === 1 && timeTyped < 6) {
      // «1:» — это «01:»: одна цифра в группе встаёт на место единиц
      timeDigits[timeTyped] = timeDigits[timeTyped - 1];
      timeDigits[timeTyped - 1] = "0";
      timeTyped++;
    }
    drawTime();
  });
  timeInput.addEventListener("paste", (event) => {
    event.preventDefault();
    const text = (event.clipboardData || window.clipboardData).getData("text");
    if (fillTime(text)) {
      timeField.classList.remove("invalid");
      drawTime();
    }
  });
  // каретка всегда за набранным: щелчок и фокус её туда и ставят (выделение мышью — можно)
  const placeCaret = () => setTimeout(() => {
    if (document.activeElement === timeInput && timeInput.selectionStart === timeInput.selectionEnd) drawTime();
  }, 0);
  timeInput.addEventListener("focus", placeCaret);
  timeInput.addEventListener("mouseup", placeCaret);
  timeInput.addEventListener("input", drawTime);   // что-то вставилось мимо keydown — вернуть маску
  drawTime();

  // Ctrl+F — к поиску, как в браузере: курсор в поле, и поле на миг подсвечено
  function onKey(event) {
    if ((event.ctrlKey || event.metaKey) && (event.code === "KeyF" || event.key.toLowerCase() === "f")) {
      event.preventDefault();
      event.stopPropagation();
      searchInput.focus();
      searchInput.select();
      searchBox.classList.remove("flash");
      void searchBox.offsetWidth;          // анимация заново, даже если уже была
      searchBox.classList.add("flash");
    }
  }
  document.addEventListener("keydown", onKey, true);

  // ---------------------------------------------------------------- график активности

  function drawActivity(answer) {
    const counts = answer.counts || [];
    activity.hidden = !counts.some(Boolean);
    if (activity.hidden) return;
    const top = Math.max(...counts);
    bars.setAttribute("viewBox", "0 0 " + counts.length + " 100");
    let path = "";
    counts.forEach((count, index) => {
      if (count) path += "M" + index + " 100V" + (100 - count / top * 100).toFixed(1) + "h0.85V100z";
    });
    const shape = document.createElementNS(SVG, "path");
    shape.setAttribute("d", path);
    bars.replaceChildren(shape);
    const total = chat.length || counts.length * answer.bucket;
    peaks.replaceChildren(...(answer.peaks || []).map((peak) => {
      const at = peak * answer.bucket;
      const mark = h("button", { type: "button", class: "chatview-peak",
        title: t("Всплеск чата: {count} за {length}", { count: fmt.counted(counts[peak], t("сообщение|сообщения|сообщений")),
                                                       length: fmt.duration(answer.bucket) }) });
      mark.style.left = Math.min(100, (at + answer.bucket / 2) / total * 100) + "%";
      mark.addEventListener("click", (event) => {
        event.stopPropagation();
        jumpTo(at);
      });
      return mark;
    }));
  }

  const GAP_TEXTS = {
    recorded: "Пропуск в чате {from}–{to}: связь с чатом прерывалась при записи.",
    start: "Возможный пропуск {from}–{to}: чат начинается позже видео.",
    end: "Возможный пропуск {from}–{to}: чат кончается раньше видео.",
    silence: "Возможный пропуск {from}–{to}: чат молчал дольше обычного для этого места.",
  };
  function drawGaps() {
    const total = chat.length || 0;
    gapMarks.replaceChildren(...(total ? chat.gaps || [] : []).map(([from, to, kind]) => {
      const mark = h("div", { class: "chatview-gap" + (kind === "recorded" ? " exact" : ""),
        title: t(GAP_TEXTS[kind] || GAP_TEXTS.silence, { from: fmt.duration(from), to: fmt.duration(to) }) + " "
               + t("Докачать можно в меню «⋯» записи в библиотеке.") });
      mark.style.left = Math.min(100, from / total * 100) + "%";
      mark.style.width = Math.max(0.3, (to - from) / total * 100) + "%";
      return mark;
    }));
  }

  const timeAt = (event) => {
    const rect = activity.getBoundingClientRect();
    return Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * (chat.length || 0);
  };
  function showHover(event) {
    const rect = activity.getBoundingClientRect();
    hoverTime.hidden = false;
    setText(hoverTime, fmt.duration(timeAt(event)));
    hoverTime.style.left = Math.max(24, Math.min(rect.width - 24, event.clientX - rect.left)) + "px";
  }

  // протяжка: зажал на графике и ведёшь — лента идёт следом. Запросы не копятся:
  // пока грузится одно место, запоминается только последнее
  let dragging = false, scrubTarget = null, scrubBusy = false;
  async function scrubTo(seconds) {
    scrubTarget = seconds;
    if (scrubBusy) return;
    scrubBusy = true;
    try {
      while (scrubTarget !== null && !closed) {
        const target = scrubTarget;
        scrubTarget = null;
        await jumpTo(target, null, true, true);
      }
    } finally {
      scrubBusy = false;
    }
    if (!dragging) afterScrub();
  }
  // отпустили — догрузить ленту вокруг места, черта — к ней
  function afterScrub() {
    if (closed) return;
    if (list.scrollTop < EDGE) loadBefore();
    else if (list.scrollHeight - list.scrollTop - list.clientHeight < EDGE) loadAfter();
    showPosition();
  }
  function scrubAt(event) {
    const seconds = timeAt(event);
    showHover(event);
    cursor.hidden = false;
    cursor.style.left = seconds / chat.length * 100 + "%";
    scrubTo(seconds);
  }

  activity.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || !chat || !chat.length || event.target.closest(".chatview-peak")) return;
    event.preventDefault();               // не выделять текст при протяжке
    activity.setPointerCapture(event.pointerId);
    dragging = true;
    activity.classList.add("dragging");
    if (mode !== "feed") {
      searchInput.value = "";
      ++searchGeneration;
    }
    scrubAt(event);
  });
  activity.addEventListener("pointermove", (event) => {
    if (!chat || !chat.length) return;
    if (dragging) scrubAt(event); else showHover(event);
  });
  const stopDrag = () => {
    if (!dragging) return;
    dragging = false;
    activity.classList.remove("dragging");
    // лента уже на месте — догрузка вокруг; ещё грузится — сделает scrubTo по окончании
    if (!scrubBusy) afterScrub();
    if (!activity.matches(":hover")) hoverTime.hidden = true;
  };
  activity.addEventListener("pointerup", stopDrag);
  activity.addEventListener("pointercancel", stopDrag);
  activity.addEventListener("pointerleave", () => { if (!dragging) hoverTime.hidden = true; });

  // ---------------------------------------------------------------- открыть

  async function openPart(part, start, mark) {
    current = part;
    ++generation;
    list.replaceChildren(h("div", { class: "spinner" }));
    try {
      chat = await bridge.call("open_chat", part.path);
    } catch (error) {
      if (!closed) showProblem(error.message || String(error));
      return;
    }
    if (closed) return;
    showProblem(null);
    setText(meta, [channelName(item.channel_id, item.channel), platformName(item.platform), fmt.date(item.timestamp),
                   fmt.counted(chat.count, t("сообщение|сообщения|сообщений")),
                   chat.length ? t("длина {length}", { length: fmt.duration(chat.length) }) : ""]
      .filter(Boolean).join(" · "));
    // история из стороннего архива, а не с Twitch: метка в самом файле чата
    source.hidden = !chat.source;
    setText(source, chat.source_part
      ? t("Чат начала эфира загружен из стороннего архива {site}, не с Twitch", { site: chat.source })
      : t("История чата загружена из стороннего архива {site}, не с Twitch", { site: chat.source }));
    drawGaps();
    bridge.call("chat_activity", chat.id, chat.length || 1).then((answer) => { if (!closed) drawActivity(answer); })
      .catch(() => { activity.hidden = true; });
    showFeed();
    await jumpTo(start || 0, mark);
    if (options.user) {
      addUser(options.user);
      options.user = null;
    } else {
      showUsers();                      // другая часть эфира — сообщения выбранных из неё
    }
  }
  function showProblem(reason) {
    // другая часть эфира может открыться — переключатель частей остаётся
    // окно — небольшое, как обычный диалог: без пустой высоты и кнопок по углам
    const several = parts.length > 1;
    for (const node of [searchBox, by, timeBox, activity, main]) node.hidden = Boolean(reason);
    head.hidden = Boolean(reason) && !several;
    tools.hidden = Boolean(reason) && !several;
    modal.body.classList.toggle("problem", Boolean(reason) && !several);
    problem.hidden = !reason;
    if (!reason) return;
    const missing = reason === t("у этой записи нет чата");
    problem.replaceChildren(
      h("strong", { text: missing ? t("Чата нет") : t("Чат не открылся") }),
      h("p", { text: missing ? t("Файла чата рядом с записью нет: его не записали, перенесли или удалили. Скачать чат можно в меню «⋯» записи в библиотеке.")
                             : reason }),
      h("div", { class: "chatview-problem-actions" },
        button(item.video_kind === "chat" ? t("Сводка чата") : t("Сводка эфира"), { icon: "stats", kind: "ghost",
          onclick: () => openSummary(item) }),
        button(t("Закрыть"), { onclick: () => modal.close() })));
  }

  partSelect.addEventListener("change", () => openPart(parts[Number(partSelect.value)] || parts[0], 0));
  partSelect.value = String(Math.max(0, parts.indexOf(current)));
  showUsers();
  allChat.hidden = true;
  openPart(current, options.start, options.mark);
  // курсор не в поиске, а в ленте: стрелки листают, Ctrl+F — к поиску
  setTimeout(() => list.focus({ preventScroll: true }), 0);
  return modal;
}

// число сообщений одной строкой — для карточек и подписей
export function messagesText(count) {
  return fmt.count(count) + " " + tp(count, t("сообщение|сообщения|сообщений"));
}
