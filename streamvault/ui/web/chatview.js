/*
 * StreamVault, окно программы: чат записи без видео — читать прямо в окне,
 * а не в браузере через .chat.html.
 *
 * Сверху — график активности: где чат писал больше всего; щелчок по нему —
 * к этому месту. Лента сообщений со смайлами и значками догружается при
 * прокрутке в обе стороны. Поиск по словам или нику; щелчок по нику — все
 * сообщения этого зрителя; щелчок по найденному — к нему в ленте.
 */

import { bridge, button, channelName, displayTitle, failed, fmt, h, openModal, platformName, setText, t, tp }
  from "./common.js";
import { openSummary } from "./summary.js";

const CHUNK = 600;            // секунд чата за один запрос
const KEEP_CHUNKS = 8;        // столько кусков в ленте, дальние убираются
const EDGE = 800;             // пикселей до края ленты — пора догружать
const MIN_LINES = 80;         // после перехода — хотя бы столько строк, чтобы было что листать
const BEFORE = 20;            // переход к месту: показать и пару десятков секунд до него
const SVG = "http://www.w3.org/2000/svg";

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
  const searchInput = h("input", { type: "search", class: "chatview-search", spellcheck: "false",
                                   placeholder: t("Слово, фраза или смайл") });
  const by = h("select", { title: t("Где искать") },
    h("option", { value: "text", text: t("в сообщениях") }), h("option", { value: "name", text: t("по нику") }));
  const timeInput = h("input", { type: "text", class: "time chatview-time", spellcheck: "false",
                                 placeholder: "1:23:45", title: t("Перейти к времени: Enter") });
  const partSelect = h("select", { title: t("Часть эфира") }, parts.map((part, index) =>
    h("option", { value: String(index), text: t("часть {part} из {count}", { part: index + 1, count: parts.length }) })));
  partSelect.hidden = parts.length < 2;
  const activity = h("div", { class: "chatview-activity", title: t("Активность чата — щелчок переносит к этому месту") });
  const bars = document.createElementNS(SVG, "svg");
  bars.setAttribute("preserveAspectRatio", "none");
  bars.classList.add("chatview-bars");
  const cursor = h("div", { class: "chatview-cursor" });
  const hoverTime = h("div", { class: "chatview-hover" });
  hoverTime.hidden = true;
  const peaks = h("div", { class: "chatview-peaks" });
  activity.append(bars, peaks, cursor, hoverTime);
  const status = h("span", { class: "chatview-status" });
  const filterChip = h("button", { type: "button", class: "chatview-chip" });
  filterChip.hidden = true;
  const position = h("span", { class: "chatview-position" });
  const list = h("div", { class: "chatview-list" }, h("div", { class: "spinner" }));
  const found = h("div", { class: "chatview-list" });
  found.hidden = true;

  modal.body.append(
    h("h2", { text: displayTitle(item.title) || item.title }), meta,
    h("div", { class: "toolbar chatview-tools" }, searchInput, by, timeInput, partSelect, h("span", { class: "spacer" }),
      button(item.video_kind === "chat" ? t("Сводка чата") : t("Сводка эфира"), { icon: "stats", kind: "ghost",
        onclick: () => openSummary(item) }),
      current.chat && current.chat.html ? button(t("Открыть в браузере"), { icon: "external", kind: "ghost",
        onclick: () => bridge.call("open_file", current.chat.html).catch(failed) }) : null),
    activity,
    h("div", { class: "chatview-bar" }, status, filterChip, h("span", { class: "spacer" }), position),
    list, found);

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
      showUser(name);
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

  // перейти к месту: лента заново, от чуть раньше него
  async function jumpTo(seconds, mark) {
    const mine = ++generation;
    seconds = Math.max(0, Math.min(seconds, chat.length || seconds));
    showFeed();
    list.replaceChildren(h("div", { class: "spinner" }));
    chunks = [];
    let from = Math.max(0, seconds - BEFORE);
    let to = from;
    const nodes = [];
    loading = true;
    try {
      // реже пишут — берём кусками, пока не наберётся, что листать
      do {
        const messages = await fetch(to, to + CHUNK);
        if (mine !== generation || closed) return;
        nodes.push(...messages.map((message) => line(message)));
        to += CHUNK;
      } while (nodes.length < MIN_LINES && to < (chat.length || 0));
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
    // сверху пусто — догрузить и туда, чтобы можно было листать назад
    if (from > 0 && list.scrollTop < EDGE) loadBefore();
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

  function showFeed() {
    found.hidden = true;
    list.hidden = false;
    filterChip.hidden = true;
    setText(status, fmt.counted(chat.count, t("сообщение|сообщения|сообщений")));
  }

  function showFound(messages, text) {
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
      filterChip.hidden = true;
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

  function showUser(name) {
    searchInput.value = "";
    ++searchGeneration;
    bridge.call("chat_search", chat.id, name, "user").then((messages) => {
      if (closed) return;
      showFound(messages, t("Сообщения {name}: {count}", { name: name, count: fmt.count(messages.length) }));
      setText(filterChip, "× " + t("ко всему чату"));
      filterChip.hidden = false;
    }).catch(failed);
  }
  filterChip.addEventListener("click", () => {
    showFeed();
    showPosition();
  });

  timeInput.addEventListener("keydown", (event) => {
    if (event.key !== "Enter") return;
    event.preventDefault();
    const seconds = parseTime(timeInput.value);
    if (seconds === null) {
      timeInput.classList.add("invalid");
      return;
    }
    timeInput.classList.remove("invalid");
    searchInput.value = "";
    jumpTo(seconds);
  });

  // Ctrl+F — к поиску, как в браузере
  function onKey(event) {
    if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "f") {
      event.preventDefault();
      searchInput.focus();
      searchInput.select();
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

  const timeAt = (event) => {
    const rect = activity.getBoundingClientRect();
    return Math.max(0, Math.min(1, (event.clientX - rect.left) / rect.width)) * (chat.length || 0);
  };
  activity.addEventListener("mousemove", (event) => {
    if (!chat || !chat.length) return;
    const rect = activity.getBoundingClientRect();
    hoverTime.hidden = false;
    setText(hoverTime, fmt.duration(timeAt(event)));
    hoverTime.style.left = Math.max(24, Math.min(rect.width - 24, event.clientX - rect.left)) + "px";
  });
  activity.addEventListener("mouseleave", () => { hoverTime.hidden = true; });
  activity.addEventListener("click", (event) => {
    if (!chat || !chat.length) return;
    searchInput.value = "";
    jumpTo(timeAt(event));
  });

  // ---------------------------------------------------------------- открыть

  async function openPart(part, start, mark) {
    current = part;
    ++generation;
    list.replaceChildren(h("div", { class: "spinner" }));
    try {
      chat = await bridge.call("open_chat", part.path);
    } catch (error) {
      list.replaceChildren(h("div", { class: "warn-text", text: error.message || String(error) }));
      return;
    }
    if (closed) return;
    setText(meta, [channelName(item.channel_id, item.channel), platformName(item.platform), fmt.date(item.timestamp),
                   fmt.counted(chat.count, t("сообщение|сообщения|сообщений")),
                   chat.length ? t("длина {length}", { length: fmt.duration(chat.length) }) : ""]
      .filter(Boolean).join(" · "));
    bridge.call("chat_activity", chat.id, chat.length || 1).then((answer) => { if (!closed) drawActivity(answer); })
      .catch(() => { activity.hidden = true; });
    await jumpTo(start || 0, mark);
    if (options.user) {
      showUser(options.user);
      options.user = null;
    }
  }
  partSelect.addEventListener("change", () => openPart(parts[Number(partSelect.value)] || parts[0], 0));
  partSelect.value = String(Math.max(0, parts.indexOf(current)));
  openPart(current, options.start, options.mark);
  setTimeout(() => searchInput.focus(), 0);
  return modal;
}

// число сообщений одной строкой — для карточек и подписей
export function messagesText(count) {
  return fmt.count(count) + " " + tp(count, t("сообщение|сообщения|сообщений"));
}
