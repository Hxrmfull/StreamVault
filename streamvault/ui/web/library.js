/*
 * StreamVault, окно программы: «Библиотека» — записанное и скачанное,
 * эфиры из частей одной карточкой, отбор, порядок и место на диске.
 */

import { bridge, button, channelName, confirmDialog, displayTitle, ensureProfiles, failed, fmt, h, icon, iconButton, openModal, platformName, safeUrl, setSrc, setText, state, syncList, t, toast, tp } from "./common.js";
import { openWatch } from "./player.js";
import { openChatSearch, openSummary } from "./summary.js";
import { view } from "./app.js";

export const LIBRARY_KINDS = [["all", "Всё"], ["record", "Записанные эфиры"], ["download", "Скачанное"]];
export const VIDEO_KIND_LABELS = { stream: "эфир", vod: "запись", clip: "клип", video: "видео" };
export const LIBRARY_SORTS = [["new", "Сначала новые"], ["old", "Сначала старые"],
                       ["long", "Сначала длинные"], ["big", "Сначала большие"]];

// отбор и порядок библиотеки — на время работы окна
export function libraryFilters() {
  const filters = { platform: "", type: "", chat: false, sort: "new" };
  try { Object.assign(filters, JSON.parse(sessionStorage.getItem("library-filters") || "{}")); } catch (error) { /* по умолчанию */ }
  return filters;
}

// кадр и длительность записи эфира делает ffmpeg — не больше двух сразу,
// и только для карточек, до которых долистали
export const previews = new Map();
export const previewQueue = [];
export let previewsRunning = 0;

export function requestPreview(path, apply) {
  if (previews.has(path)) { apply(previews.get(path)); return; }
  previewQueue.push({ path: path, apply: apply });
  pumpPreviews();
}

export function pumpPreviews() {
  while (previewsRunning < 2 && previewQueue.length) {
    const { path, apply } = previewQueue.shift();
    if (previews.has(path)) { apply(previews.get(path)); continue; }
    previewsRunning++;
    bridge.call("library_preview", path)
      .then((preview) => { previews.set(path, preview); apply(preview); })
      .catch(() => previews.set(path, { thumbnail: "", duration: null }))
      .finally(() => { previewsRunning--; pumpPreviews(); });
  }
}

export let libraryReloadTimer = null;
export function scheduleLibraryReload() {
  if (state.route.name !== "library") return;
  clearTimeout(libraryReloadTimer);
  libraryReloadTimer = setTimeout(() => { if (state.route.name === "library") loadLibrary(); }, 1500);
}

export function renderLibrary(main) {
  const remembered = sessionStorage.getItem("library-kind");
  view.kind = LIBRARY_KINDS.some(([kind]) => kind === remembered) ? remembered : "all";
  view.items = null;
  view.summary = h("span", { class: "muted" });
  view.tabs = h("div", { class: "tabs" });
  for (const [kind, label] of LIBRARY_KINDS) {
    const tab = h("button", { type: "button", text: t(label) });
    tab.dataset.kind = kind;
    tab.addEventListener("click", () => {
      view.kind = kind;
      sessionStorage.setItem("library-kind", kind);
      showLibrary();
    });
    view.tabs.append(tab);
  }
  view.channelSelect = h("select", { title: t("Канал") });
  view.channelSelect.addEventListener("change", showLibrary);
  view.search = h("input", { type: "text", class: "library-search", placeholder: t("Поиск по названию и каналу"),
                             spellcheck: "false" });
  view.search.addEventListener("input", showLibrary);
  // отбор: площадка, тип, есть ли чат; порядок — по дате, длительности, размеру
  view.filters = libraryFilters();
  const saveFilters = () => {
    view.filters = { platform: view.platformSelect.value, type: view.typeSelect.value,
                     chat: view.chatOnly.checked, sort: view.sortSelect.value };
    sessionStorage.setItem("library-filters", JSON.stringify(view.filters));
    showLibrary();
  };
  view.platformSelect = h("select", { title: t("Площадка") }, h("option", { value: "", text: t("Все площадки") }),
    Object.keys(state.info.sites || {}).map((platform) => h("option", { value: platform, text: platformName(platform) })));
  view.typeSelect = h("select", { title: t("Тип") }, h("option", { value: "", text: t("Любой тип") }),
    Object.entries(VIDEO_KIND_LABELS).map(([kind, label]) => h("option", { value: kind, text: t(label) })));
  view.chatOnly = h("input", { type: "checkbox" });
  view.sortSelect = h("select", { title: t("Порядок") },
    LIBRARY_SORTS.map(([sort, label]) => h("option", { value: sort, text: t(label) })));
  view.platformSelect.value = view.filters.platform;
  view.typeSelect.value = view.filters.type;
  view.chatOnly.checked = Boolean(view.filters.chat);
  view.sortSelect.value = view.filters.sort;
  for (const control of [view.platformSelect, view.typeSelect, view.chatOnly, view.sortSelect]) {
    control.addEventListener("change", saveFilters);
  }
  view.grid = h("div", { class: "grid" });
  view.status = h("div", null, h("div", { class: "spinner" }));
  view.observer = new IntersectionObserver((entries) => {
    for (const entry of entries) {
      if (!entry.isIntersecting) continue;
      view.observer.unobserve(entry.target);
      if (entry.target._loadPreview) entry.target._loadPreview();
    }
  }, { root: main, rootMargin: "400px" });

  main.append(
    h("div", { class: "toolbar" },
      h("h1", { class: "page-title", text: t("Библиотека") }), view.summary, h("span", { class: "spacer" }),
      button(t("Папка записей"), { icon: "folder", onclick: () => bridge.call("open_folder", "recordings").catch(failed) }),
      button(t("Папка загрузок"), { icon: "folder", onclick: () => bridge.call("open_folder", "downloads").catch(failed) }),
      button(t("Поиск по чатам"), { icon: "search", title: t("Найти слово или ник во всех чатах библиотеки"),
                                    onclick: () => { if (view.items) openChatSearch(view.items); } }),
      iconButton("refresh", t("Перечитать папки"), () => loadLibrary())),
    h("div", { class: "toolbar" }, view.tabs, h("span", { class: "spacer" }), view.channelSelect, view.search),
    h("div", { class: "toolbar library-filters" }, view.platformSelect, view.typeSelect,
      h("label", { class: "switch" }, view.chatOnly, h("span", { class: "track" }), t("есть чат")),
      view.sortSelect, h("span", { class: "spacer" }),
      button(t("Место на диске"), { icon: "folder", onclick: () => openDiskUsage() })),
    view.status, view.grid);
  view.refresh = null;
  loadLibrary();
}

export async function loadLibrary() {
  const shown = view;
  try {
    const data = await bridge.call("library");
    if (view !== shown) return;
    view.items = data.items;
    view.free = data.free || {};
  } catch (error) {
    if (view !== shown) return;
    view.status.replaceChildren(h("div", { class: "empty" },
      h("strong", { text: t("Библиотека не прочиталась") }), h("span", { text: error.message })));
    return;
  }
  fillLibraryChannels();
  // пришли имена каналов Twitch — подписи карточек и списка каналов меняются
  view.onProfiles = () => { fillLibraryChannels(); showLibrary(); };
  ensureProfiles([...new Set(view.items.map((item) => item.channel_id))]);
  showLibrary();
}

export function fillLibraryChannels() {
  const channels = new Map();
  for (const item of view.items) {
    channels.set(item.channel_id, channelName(item.channel_id, item.channel) + " · " + platformName(item.platform));
  }
  const selected = view.channelSelect.value;
  view.channelSelect.replaceChildren(h("option", { value: "", text: t("Все каналы") }),
    [...channels].sort((a, b) => a[1].localeCompare(b[1], "ru"))
      .map(([id, label]) => h("option", { value: id, text: label })));
  view.channelSelect.value = channels.has(selected) ? selected : "";
}

// длительность записи: из списка или из превью, которое уже сделано
export function knownDuration(item) {
  return item.duration || (previews.get(item.path) || {}).duration || null;
}

// части одного эфира — одной карточкой: общий размер, длительность, части по порядку
export function groupParts(items) {
  const shown = [], groups = new Map();
  for (const item of items) {
    if (!item.broadcast) {
      shown.push(item);
      continue;
    }
    let group = groups.get(item.broadcast);
    if (!group) {
      group = [];
      groups.set(item.broadcast, group);
      shown.push(group);
    }
    group.push(item);
  }
  return shown.map((entry) => {
    if (!Array.isArray(entry)) return entry;
    // части после обрывов, внутри них — куски поделённой при конвертации записи
    const parts = entry.sort((a, b) => (a.part || 1) - (b.part || 1) || (a.piece || 0) - (b.piece || 0));
    if (parts.length === 1) return parts[0];
    return Object.assign({}, parts[0], {
      key: "broadcast:" + parts[0].broadcast, parts: parts,
      size: parts.reduce((sum, part) => sum + part.size, 0),
      timestamp: Math.min(...parts.map((part) => part.timestamp || Infinity)),
      chat: (parts.find((part) => part.chat && part.chat.json) || parts[0]).chat,
    });
  });
}

export function partsOf(item) { return item.parts || [item]; }

export function groupDuration(item) {
  const durations = partsOf(item).map(knownDuration);
  return durations.every(Boolean) ? durations.reduce((sum, seconds) => sum + seconds, 0) : null;
}

export function showLibrary() {
  for (const tab of view.tabs.children) tab.classList.toggle("active", tab.dataset.kind === view.kind);
  if (!view.items) return;
  const query = view.search.value.trim().toLowerCase();
  const channel = view.channelSelect.value;
  const filters = view.filters;
  const items = groupParts(view.items).filter((item) =>
    (view.kind === "all" || item.kind === view.kind)
    && (!channel || item.channel_id === channel)
    && (!filters.platform || item.platform === filters.platform)
    && (!filters.type || (item.video_kind || "video") === filters.type)
    && (!filters.chat || partsOf(item).some((part) => part.chat && part.chat.json))
    && (!query || item.title.toLowerCase().includes(query) || item.channel.toLowerCase().includes(query)));
  const order = {
    new: (a, b) => (b.timestamp || 0) - (a.timestamp || 0),
    old: (a, b) => (a.timestamp || 0) - (b.timestamp || 0),
    // длительность ещё не узнанной записи — в конце, пока превью её не скажет
    long: (a, b) => (groupDuration(b) || 0) - (groupDuration(a) || 0),
    big: (a, b) => b.size - a.size,
  }[filters.sort] || ((a, b) => (b.timestamp || 0) - (a.timestamp || 0));
  items.sort(order);
  const files = items.reduce((sum, item) => sum + partsOf(item).length, 0);
  const total = items.reduce((sum, item) => sum + item.size, 0);
  setText(view.summary, items.length
    ? files + " " + tp(files, t("файл|файла|файлов")) + " · " + fmt.size(total) : "");
  view.status.replaceChildren();
  if (!view.items.length) {
    view.status.append(h("div", { class: "empty" }, h("strong", { text: t("Здесь пока пусто") }),
      t("Сюда попадают готовые записи эфиров и всё скачанное.")));
  } else if (!items.length) {
    view.status.append(h("div", { class: "empty", text: t("Ничего не нашлось.") }));
  }
  syncList(view.grid, items, (item) => item.key || item.path, createLibraryCard, updateLibraryCard);
}

// сколько занимают записи: всего, по каналам и сколько свободно на диске
export function openDiskUsage() {
  if (!view.items) return;
  const modal = openModal({ wide: true });
  const channels = new Map();
  let records = 0, downloads = 0;
  for (const item of view.items) {
    if (item.kind === "record") records += item.size; else downloads += item.size;
    const row = channels.get(item.channel_id) || { id: item.channel_id, channel: item.channel, platform: item.platform,
                                                   size: 0, files: 0 };
    row.size += item.size;
    row.files += 1;
    channels.set(item.channel_id, row);
  }
  const rows = [...channels.values()].sort((a, b) => b.size - a.size);
  const largest = rows.length ? rows[0].size || 1 : 1;
  const free = view.free || {};
  const stat = (label, value) => h("div", { class: "usage-stat" },
    h("div", { class: "muted", text: label }), h("strong", { text: value }));
  modal.body.append(
    h("h2", { text: t("Место на диске") }),
    h("div", { class: "usage-stats" },
      stat(t("Всего"), fmt.size(records + downloads)),
      stat(t("Записанные эфиры"), fmt.size(records)),
      stat(t("Скачанное"), fmt.size(downloads)),
      free.recordings !== null && free.recordings !== undefined
        ? stat(t("Свободно на диске записей"), fmt.size(free.recordings)) : null),
    rows.length ? h("div", { class: "usage-list" }, rows.map((row) => {
      const bar = h("span", { class: "usage-fill" });
      bar.style.width = Math.max(1, Math.round(row.size / largest * 100)) + "%";
      const line = h("button", { type: "button", class: "usage-row", title: t("Показать записи канала") },
        h("span", { class: "usage-name" }, channelName(row.id, row.channel),
          h("span", { class: "muted", text: " · " + platformName(row.platform) })),
        h("span", { class: "usage-bar" }, bar),
        h("span", { class: "usage-size", text: fmt.size(row.size) }),
        h("span", { class: "muted usage-files", text: row.files + " " + tp(row.files, t("файл|файла|файлов")) }));
      line.addEventListener("click", () => {
        view.channelSelect.value = row.id;
        modal.close();
        showLibrary();
      });
      return line;
    })) : h("div", { class: "muted", text: t("Здесь пока пусто") }));
}

export function createLibraryCard() {
  const card = h("div", { class: "card" });
  card._img = h("img", { alt: "" });
  card._img.hidden = true;
  card._kind = h("span", { class: "badge dark" });
  card._ts = h("span", { class: "badge dark", text: ".ts", title: t("Запись не сконвертирована в mp4") });
  card._duration = h("span", { class: "badge dark" });
  card._size = h("span", { class: "badge dark" });
  card._parts = h("span", { class: "badge dark" });
  // в записи нашлись пропуски — видно сразу, подробности в сводке
  card._gaps = h("span", { class: "badge warn" });
  card._title = h("div", { class: "card-title" });
  card._sub = h("div", { class: "card-sub" });
  // значок «играть» — проигрыватель Windows; щелчок по карточке — проигрыватель в окне
  card._watch = iconButton("play", t("Открыть в проигрывателе"), (event) => {
    event.stopPropagation();
    bridge.call("open_file", card._item.path).catch(failed);
  });
  card._chatOpen = iconButton("chat", t("Открыть чат"), (event) => {
    event.stopPropagation();
    bridge.call("open_file", card._item.chat.html).catch(failed);
  });
  // видео чата поверх записи — новым файлом рядом
  card._overlay = iconButton("overlay", t("Видео с чатом поверх"), (event) => {
    event.stopPropagation();
    openOverlay(card._item);
  });
  card._chatVideo = iconButton("video", t("Открыть видео чата"), (event) => {
    event.stopPropagation();
    bridge.call("open_file", card._item.chat.video).catch(failed);
  });
  card._convert = iconButton("convert", t("Конвертировать в mp4"), (event) => {
    event.stopPropagation();
    card._convert.disabled = true;
    for (const item of partsOf(card._item).filter((part) => /\.ts$/i.test(part.path))) {
      state.converting.add(item.path);
      bridge.call("convert_recording", item.path)
        .then(() => toast(t("Конвертирую в mp4: {name}", { name: item.name }), "ok"))
        .catch((error) => {
          state.converting.delete(item.path);
          card._convert.disabled = false;
          failed(error);
        });
    }
  });
  card._summary = iconButton("stats", t("Сводка эфира"), (event) => {
    event.stopPropagation();
    openSummary(card._item);
  });
  // действия — на кадре при наведении: название получает всю ширину карточки
  const actions = h("div", { class: "card-actions overlay corner top-right" }, card._watch, card._summary, card._convert,
    card._chatOpen, card._chatVideo, card._overlay,
    iconButton("folder", t("Показать в папке"), (event) => {
      event.stopPropagation();
      bridge.call("show_in_folder", card._item.path).catch(failed);
    }),
    iconButton("trash", t("В корзину"), (event) => {
      event.stopPropagation();
      confirmDelete(card._item);
    }));
  // эфир из частей — с первой, дальше они идут подряд
  card.addEventListener("click", () => openWatch(partsOf(card._item)[0]));
  card.append(
    h("div", { class: "thumb" }, h("div", { class: "placeholder" }, icon("video")), card._img,
      h("div", { class: "corner top-left" }, card._kind, card._parts, card._ts, card._gaps),
      actions,
      h("div", { class: "corner bottom-left" }, card._duration),
      h("div", { class: "corner bottom-right" }, card._size)),
    h("div", { class: "card-body" }, h("div", { class: "card-text" }, card._title, card._sub)));
  return card;
}

export function updateLibraryCard(card, item) {
  card._item = item;
  const parts = partsOf(item);
  const chatFiles = item.chat || {};
  // смотреть в окне можно любую запись: с чатом он идёт рядом, а клип вырезается и без него
  const withChat = parts.some((part) => part.chat && part.chat.json);
  card.title = withChat ? t("Смотреть с чатом") : t("Смотреть в окне");
  card._overlay.hidden = Boolean(item.edit) || !parts.some((part) => part.chat && part.chat.video);
  card._chatOpen.hidden = !chatFiles.html;
  card._chatVideo.hidden = !chatFiles.video;
  // запись, оставленная в .ts (автоконвертация выключена), — в mp4 по кнопке
  const ts = parts.filter((part) => /\.ts$/i.test(part.path));
  card._convert.hidden = !(item.kind === "record" && ts.length);
  card._ts.hidden = !ts.length;
  card._convert.disabled = ts.length > 0 && ts.every((part) => state.converting.has(part.path));
  card._parts.hidden = parts.length < 2;
  const gaps = parts.map((part) => part.gaps).filter(Boolean);
  const gapCount = gaps.reduce((sum, part) => sum + part.count, 0);
  const lost = gaps.reduce((sum, part) => sum + part.lost, 0);
  card._gaps.hidden = !gapCount;
  setText(card._gaps, gapCount + " " + tp(gapCount, t("пропуск|пропуска|пропусков")));
  card._gaps.title = lost ? t("В записи нет кусков эфира: пропало {length}", { length: fmt.duration(lost) })
                          : t("В записи нет кусков эфира");
  setText(card._parts, parts.length + " " + tp(parts.length, t("часть|части|частей")));
  setText(card._kind, item.edit === "chat" ? t("с чатом поверх")
                                            : t(VIDEO_KIND_LABELS[item.video_kind] || t("скачано")));
  setText(card._size, fmt.size(item.size));
  setText(card._title, displayTitle(item.title) || item.title);
  card._title.title = parts.map((part) => part.name).join("\n");
  setText(card._sub, [channelName(item.channel_id, item.channel), platformName(item.platform),
                      fmt.date(item.timestamp),
                      !item.parts && item.part ? t("часть {part}", { part: item.part }) : ""]
                     .filter(Boolean).join(" · "));
  const showDuration = (seconds) => {
    card._duration.hidden = !seconds;
    setText(card._duration, fmt.duration(seconds));
  };
  // длительность эфира из частей — когда известна у каждой
  const duration = () => (item.parts ? groupDuration(item) : knownDuration(item));
  showDuration(duration());

  if (safeUrl(item.thumbnail)) {
    setSrc(card._img, item.thumbnail);
    return;
  }
  // превью с сайта нет — кадр из самого файла (первой части), когда карточка
  // покажется; у частей эфира — ещё и их длительность
  const key = item.key || item.path;
  const first = parts[0];
  if (previews.has(first.path)) setSrc(card._img, previews.get(first.path).thumbnail);
  const missing = parts.filter((part) => !previews.has(part.path) && (part === first || !knownDuration(part)));
  if (missing.length && card._watched !== key) {
    card._watched = key;
    card._loadPreview = () => missing.forEach((part) => requestPreview(part.path, (preview) => {
      if ((card._item.key || card._item.path) !== key) return;
      if (part === first) setSrc(card._img, preview.thumbnail);
      showDuration(duration());
    }));
    view.observer.observe(card);
  }
}

// видео чата поверх записи: справа или слева, перекодирование всего видео.
// У эфира из частей — каждая часть, у которой есть видео чата
function openOverlay(item) {
  const targets = partsOf(item).filter((part) => part.chat && part.chat.video);
  const modal = openModal();
  const start = async (side) => {
    modal.close();
    let started = 0;
    for (const part of targets) {
      try {
        await bridge.call("overlay_chat", part.path, side);
        started++;
      } catch (error) {
        failed(error);
      }
    }
    if (started) toast(t("Накладываю чат на видео — ход виден в «Загрузках»"), "ok", "#downloads");
  };
  modal.body.append(
    h("h2", { text: t("Видео с чатом поверх") }),
    h("div", { class: "muted", text: t("Видео чата ляжет поверх записи — новым файлом рядом с ней, запись не меняется. " +
      "Это перекодирование всего видео: час записи — от нескольких минут до получаса, смотря какой компьютер.") }),
    h("div", { class: "actions" },
      button(t("Отмена"), { onclick: () => modal.close() }),
      button(t("Чат слева"), { onclick: () => start("left") }),
      button(t("Чат справа"), { kind: "primary", onclick: () => start("right") })));
}

export function confirmDelete(item) {
  const parts = partsOf(item);
  const withChat = parts.some((part) => Object.keys(part.chat || {}).length > 0);
  const name = parts.length > 1
    ? t("{title} — все части ({count})", { title: displayTitle(item.title) || item.title, count: parts.length })
    : item.name;
  confirmDialog(t("Отправить в корзину?"), withChat ? name + " — " + t("вместе с чатом") : name,
    t("В корзину"), async () => {
      const removed = new Set();
      for (const part of parts) {
        try {
          await bridge.call("delete_file", part.path);
          removed.add(part.path);
        } catch (error) {
          failed(error);
        }
      }
      if (removed.size) toast(t("Отправлено в корзину — его можно вернуть оттуда"), "ok");
      if (view.items) {
        view.items = view.items.filter((other) => !removed.has(other.path));
        showLibrary();
      }
    });
}
