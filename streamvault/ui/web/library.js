/*
 * StreamVault, окно программы: «Библиотека» — записанное и скачанное,
 * эфиры из частей одной карточкой, отбор, порядок и место на диске.
 */

import { openActionMenu } from "./menu.js";
import { CONVERT_FORMATS, SPLIT_MODES, showEditsNext, siteInfo, bridge, chatSupported, fillChannelLine, button, channelName, confirmDialog, displayTitle, ensureProfiles, failed, fmt, h, icon, iconButton, openModal, platformName, safeUrl, setSrc, setText, state, syncList, t, toast, tp } from "./common.js";
import { joiningText } from "./downloads.js";
import { openWatch } from "./player.js";
import { openChatSearch, openSummary } from "./summary.js";
import { messagesText, openChatView } from "./chatview.js";
import { view } from "./app.js";

// «Чаты» — скачанные без видео: у них нет кадра и длительности эфира, они отдельно
export const LIBRARY_KINDS = [["all", "Всё"], ["record", "Записанные эфиры"], ["download", "Скачанное"], ["chat", "Чаты"]];
export const VIDEO_KIND_LABELS = { stream: "эфир", vod: "запись", clip: "клип", video: "видео", chat: "чат" };
// «недавно добавленные» — по тому, когда файл появился на диске; по дате эфира — отдельно:
// только что скачанная запись недельной давности иначе терялась внизу
export const LIBRARY_SORTS = [["new", "Сначала недавно добавленные"], ["stream_new", "По дате эфира: новые"],
                              ["stream_old", "По дате эфира: старые"], ["long", "Сначала длинные"],
                              ["big", "Сначала большие"]];

// отбор и порядок библиотеки — на время работы окна
export function libraryFilters() {
  const filters = { platform: "", type: "", chat: false, sort: "new" };
  try { Object.assign(filters, JSON.parse(sessionStorage.getItem("library-filters") || "{}")); } catch (error) { /* по умолчанию */ }
  // прежнего порядка (old) больше нет — по умолчанию
  if (!LIBRARY_SORTS.some(([sort]) => sort === filters.sort)) filters.sort = "new";
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
  view.summary = h("span", { class: "muted page-note" });
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
    Object.entries(VIDEO_KIND_LABELS).filter(([kind]) => kind !== "chat")    // у чатов своя вкладка
      .map(([kind, label]) => h("option", { value: kind, text: t(label) })));
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
    ...[...channels].sort((a, b) => a[1].localeCompare(b[1], "ru"))
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
      // эфир из частей добавлен, когда появилась последняя его часть
      added: Math.max(...parts.map((part) => part.added || 0)),
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
    (view.kind === "all" || (view.kind === "chat" ? item.video_kind === "chat"
                                                  : item.kind === view.kind && item.video_kind !== "chat"))
    && (!channel || item.channel_id === channel)
    && (!filters.platform || item.platform === filters.platform)
    && (!filters.type || (item.video_kind || "video") === filters.type)
    && (!filters.chat || partsOf(item).some((part) => part.chat && part.chat.json))
    && (!query || item.title.toLowerCase().includes(query) || item.channel.toLowerCase().includes(query)));
  const order = {
    new: (a, b) => (b.added || b.timestamp || 0) - (a.added || a.timestamp || 0),
    stream_new: (a, b) => (b.timestamp || 0) - (a.timestamp || 0),
    stream_old: (a, b) => (a.timestamp || 0) - (b.timestamp || 0),
    // длительность ещё не узнанной записи — в конце, пока превью её не скажет
    long: (a, b) => (groupDuration(b) || 0) - (groupDuration(a) || 0),
    big: (a, b) => b.size - a.size,
  }[filters.sort] || ((a, b) => (b.added || b.timestamp || 0) - (a.added || a.timestamp || 0));
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
  card._ts = h("span", { class: "badge dark", text: ".ts", title: t("Запись не сконвертирована") });
  card._duration = h("span", { class: "badge dark" });
  card._size = h("span", { class: "badge dark" });
  card._parts = h("span", { class: "badge dark" });
  // целая запись, оставленная рядом с кусками после «Разделить»
  card._whole = h("span", { class: "badge dark", text: t("целая"),
                            title: t("Целая запись: её части — отдельной карточкой рядом") });
  // в записи нашлись пропуски — видно сразу, подробности в сводке
  card._gaps = h("span", { class: "badge warn" });
  // склейка начала эфира с записью: идёт или не вышла — видно прямо на карточке
  card._join = h("span", { class: "badge dark" });
  // склейка идёт — полосой по низу кадра, с подписью прямо на ней
  card._joinText = h("span", { class: "card-join-text" });
  card._joinBar = h("div", { class: "progress join card-join-bar" }, h("div", { class: "bar" }), card._joinText);
  card._joinBar.hidden = true;
  card._title = h("div", { class: "card-title" });
  card._sub = h("div", { class: "card-sub" });
  // на кадре при наведении — сводка и «⋯»: остальное в меню, словами. Значки без
  // подписей путались («открыть чат» и «скачать чат» выглядели одинаково)
  card._summary = iconButton("stats", t("Сводка эфира"), (event) => {
    event.stopPropagation();
    openSummary(card._item);
  });
  card._more = iconButton("more", t("Действия"), (event) => {
    event.stopPropagation();
    openCardMenu(card, card._more);
  });
  const actions = h("div", { class: "card-actions overlay corner top-right" }, card._summary, card._more);
  // эфир из частей — с первой, дальше они идут подряд; чат без видео — окном чата
  card.addEventListener("click", () => (card._item.video_kind === "chat" ? openChatView(card._item)
                                                                         : openWatch(partsOf(card._item)[0])));
  // правый щелчок — то же меню, у места щелчка
  card.addEventListener("contextmenu", (event) => {
    event.preventDefault();
    openCardMenu(card, { x: event.clientX, y: event.clientY });
  });
  // чат без видео: вместо кадра — значок чата и его график активности
  card._chatThumb = h("div", { class: "chat-thumb" });
  card._chatThumb.hidden = true;
  card.append(
    h("div", { class: "thumb" }, h("div", { class: "placeholder" }, icon("video")), card._img, card._chatThumb,
      h("div", { class: "corner top-left" }, card._kind, card._whole, card._parts, card._ts, card._gaps, card._join),
      actions,
      h("div", { class: "corner bottom-left" }, card._duration),
      h("div", { class: "corner bottom-right" }, card._size), card._joinBar),
    h("div", { class: "card-body" }, h("div", { class: "card-text" }, card._title, card._sub)));
  return card;
}

// что можно сделать с записью карточки — считается при открытии меню, по свежему состоянию
function cardAbilities(item) {
  const parts = partsOf(item);
  const chatFiles = item.chat || {};
  const onlyChat = item.video_kind === "chat";
  const site = siteInfo(item.platform);
  const vodChat = Boolean(site) && site.chat_kinds.includes(item.platform === "youtube" ? "video" : "vod");
  const ts = parts.filter((part) => /\.ts$/i.test(part.path));
  const whole = onlyChat ? [] : splittable(item);
  // пути в «Загрузках» и в библиотеке бывают записаны по-разному: «/» и «\», регистр
  const same = (path) => String(path || "").replace(/\//g, "\\").toLowerCase();
  const paths = new Set(parts.map((part) => same(part.path)));
  return {
    onlyChat,
    chatHtml: chatFiles.html,
    // читать чат в окне программы: у записи (любой её части) или у чата без видео
    chatRead: onlyChat || parts.some((part) => part.chat && part.chat.json),
    chatVideo: chatFiles.video,
    overlay: !item.edit && parts.some((part) => part.chat && part.chat.video && !isSound(part.path)),
    // своя запись эфира, у части которой нет чата, — его можно взять с площадки
    chatFetch: item.kind === "record" && !item.edit && vodChat
      && parts.some((part) => !(part.chat && part.chat.json) && !/ - part00\.[^.]+$/i.test(part.path)),
    // скачанное видео без чата — чат той же загрузкой (как кнопка в «Загрузках»)
    downloadChat: item.kind === "download" && !onlyChat && !parts.some((part) => part.chat && part.chat.json)
      ? [...state.jobs.values()].find((job) => job.state === "done" && job.file && paths.has(same(job.file))
                                             && chatSupported(job)) || null
      : null,
    // архив чатов — у своих записей эфиров; можно ли сейчас, скажет сама программа
    archive: item.kind === "record" && !item.edit && !onlyChat,
    // начало эфира склеено с записью без своего чата — докачать его (как в «Загрузках»)
    beginningChat: [...state.jobs.values()].find((job) => job.joined && !job.beginning_chat && job.state === "done"
                                                  && paths.has(same(job.file)) && chatSupported(job)),
    // запись, оставленная в .ts (автоконвертация выключена), — сконвертировать
    convert: item.kind === "record" && ts.length > 0,
    converting: ts.length > 0 && ts.every((part) => state.converting.has(part.path)),
    split: whole.length > 0,
    // целую часть можно поделить, пока она не делится прямо сейчас
    splitting: whole.length > 0 && whole.every((part) => [...state.edits.values()].some((job) =>
      job.kind === "split" && job.source === part.path && ["queued", "running"].includes(job.state))),
  };
}

// меню карточки: смотреть · чат · файл · удалить — словами и по группам
function openCardMenu(card, at) {
  const item = card._item;
  const can = cardAbilities(item);
  const run = (action) => () => Promise.resolve().then(action).catch(failed);
  card.classList.add("menu-open");
  openActionMenu(at, [
    [can.onlyChat ? null : { icon: "play", label: t("Смотреть в окне"), onclick: () => openWatch(partsOf(item)[0]) },
     can.onlyChat ? null : { icon: "external", label: t("Открыть в проигрывателе Windows"),
                             onclick: run(() => bridge.call("open_file", item.path)) },
     { icon: "stats", label: can.onlyChat ? t("Сводка чата") : t("Сводка эфира"), onclick: () => openSummary(item) }],
    [can.chatRead ? { icon: "chat", label: t("Читать чат"), onclick: () => openChatView(item) } : null,
     can.chatHtml ? { icon: "external", label: t("Открыть чат в браузере"),
                      onclick: run(() => bridge.call("open_file", can.chatHtml)) } : null,
     can.chatVideo ? { icon: "video", label: t("Открыть видео чата"), onclick: run(() => bridge.call("open_file", can.chatVideo)) } : null,
     can.downloadChat ? { icon: "download", label: t("Скачать чат"),
                          title: t("Чат этого видео с площадки — рядом с ним; ход в «Загрузках»"),
                          onclick: run(() => bridge.call("download_chat", can.downloadChat.id).then(chatInDownloads)) } : null,
     can.chatFetch ? { icon: "download", label: t("Скачать чат с площадки"),
                       title: t("Чата при записи не было — взять его из записи эфира на площадке"),
                       onclick: () => fetchRecordingChat(item) } : null,
     // запасной путь: публичный архив чатов Twitch — когда записи эфира на Twitch нет;
     // нельзя — пункт серый, и под ним сказано почему
     // сторонний сервис, не Twitch — это сказано прямо в пункте, ещё до загрузки
     can.archive ? { icon: "download", label: t("Взять чат из стороннего архива"), checking: t("проверка архива чатов…"),
                     check: () => bridge.call("chat_archive_status", item.path).then((answer) => ({
                       disabled: !answer.ok,
                       hint: answer.ok ? [t("logs.zonian.dev, сторонний сервис, не Twitch"), answer.reason].filter(Boolean).join(". ")
                                       : answer.reason })),
                     onclick: () => fetchArchiveChat(item) } : null,
     can.beginningChat ? { icon: "download", label: t("Скачать чат начала"),
                           title: t("Чат начала эфира встанет в чат записи до стыка; чат прямого эфира останется"),
                           onclick: run(() => bridge.call("download_chat", can.beginningChat.id).then(chatInDownloads)) } : null,
     can.overlay ? { icon: "overlay", label: t("Сделать видео с чатом поверх"), onclick: () => openOverlay(item) } : null],
    [can.convert ? { icon: "convert", label: can.converting ? t("Конвертируется…") : t("Конвертировать…"),
                     disabled: can.converting, onclick: () => openConvert(card) } : null,
     can.split ? { icon: "scissors", label: can.splitting ? t("Делится на части…") : t("Разделить на части…"),
                   disabled: can.splitting, onclick: () => openSplit(item) } : null,
     { icon: "folder", label: t("Показать в папке"), onclick: run(() => bridge.call("show_in_folder", item.path)) }],
    [{ icon: "trash", label: t("В корзину"), danger: true, onclick: () => confirmDelete(item) }],
  ], () => card.classList.remove("menu-open"));
}

// чат качается в «Загрузках» — туда и откроется, и там виден его ход
function chatInDownloads(answer) {
  try { sessionStorage.setItem("downloads-tab", "downloads"); } catch (error) { /* не помним — не беда */ }
  toast(answer && answer.added > 1
    ? t("Чат скачивается: частей {count} — ход в «Загрузках»", { count: answer.added })
    : t("Чат скачивается — ход в «Загрузках»"), "ok", "#downloads");
}

// чат из архива чатов: по загрузке на каждую часть без чата — ход в «Загрузках»
async function fetchArchiveChat(item) {
  try {
    await bridge.call("recording_chat_archive", item.path);
    try { sessionStorage.setItem("downloads-tab", "downloads"); } catch (error) { /* не помним — не беда */ }
    toast(t("История чата загружается из стороннего архива logs.zonian.dev, ход в «Загрузках»"), "ok", "#downloads");
  } catch (error) {
    failed(error);
  }
}

// чата при записи не было (выключен) — взять его из записи эфира на площадке
async function fetchRecordingChat(item) {
  toast(t("Поиск записи этого эфира на площадке…"));
  try {
    chatInDownloads(await bridge.call("recording_chat", item.path));
  } catch (error) {
    failed(error);
  }
}

// настройка записи канала этой записи: своя у канала или общая
function channelSetting(item, key) {
  const channel = [...state.channels.values()].find((entry) => entry.platform === item.platform
    && String(entry.label || "").toLowerCase() === String(item.channel || "").toLowerCase());
  const options = (channel && channel.options) || {};
  return key in options ? options[key] : state.settings[key];
}

// как делить: режим и его число; сразу стоит то, что у канала. withOff — можно «не делить»
function splitControls(item, withOff) {
  const setting = (key) => channelSetting(item, key);
  const modes = SPLIT_MODES.filter(([value]) => withOff || value !== "off");
  const mode = h("select", null, modes.map(([value, label]) => h("option", { value: value, text: t(label) })));
  const chosen = setting("split_mode");
  mode.value = modes.some(([value]) => value === chosen) ? chosen : modes[0][0];
  // пределы — как у настроек канала (platforms.CHANNEL_NUMBERS)
  const number = (key, min, step) => h("input", { type: "number", min: min, step: step || "1",
                                                  value: String(setting(key)) });
  const numbers = { size: number("split_size_gb", "0.1", "0.1"), duration: number("split_minutes", "5"),
                    count: number("split_count", "2") };
  const numberFields = {
    size: field(t("Часть не больше, ГБ"), numbers.size),
    duration: field(t("Часть длиной, минут"), numbers.duration),
    count: field(t("На сколько частей"), numbers.count),
  };
  const showNumbers = () => {
    for (const [name, element] of Object.entries(numberFields)) element.hidden = name !== mode.value;
  };
  mode.addEventListener("change", showNumbers);
  showNumbers();
  return {
    fields: [field(t("Делить на части"), mode), numberFields.size, numberFields.duration, numberFields.count],
    value: () => ({ mode: mode.value, size_gb: Number(numbers.size.value.replace(",", ".")),
                    minutes: Number(numbers.duration.value), count: Number(numbers.count.value) }),
  };
}

function field(label, control) {
  return h("div", { class: "field" }, h("label", { text: label }), control);
}

// что можно разделить: не клип, не видео с чатом поверх, не кусок уже поделённого
// и не целая запись, оставленная рядом с кусками
function splittable(item) {
  // целую из whole тоже: её можно поделить заново, на другие части
  return item.edit ? [] : partsOf(item).filter((part) => !part.piece);
}

// «Разделить»: каждая целая часть — на куски того же формата; целая остаётся
// в подпапке whole (галочка, по умолчанию включена) или уходит в корзину
function openSplit(item) {
  const parts = splittable(item);
  if (!parts.length) return;
  const controls = splitControls(item, false);
  const keep = h("input", { type: "checkbox" });
  keep.checked = true;
  const keepHint = h("div", { class: "hint" });
  const again = parts.every((part) => part.whole);
  const showKeep = () => setText(keepHint, keep.checked
    ? (again ? t("Целая запись с чатом останется в папке whole; новые части лягут рядом с прежними.")
      : t("Целая запись с чатом переедет в папку whole рядом с частями и будет в библиотеке отдельной карточкой."))
    : t("Целая запись с чатом уйдёт в корзину Windows. Файл больше корзины Windows удаляет насовсем."));
  keep.addEventListener("change", showKeep);
  showKeep();
  const modal = openModal();
  const start = button(t("Разделить"), { kind: "primary", onclick: async () => {
    start.disabled = true;
    let started = 0;
    for (const part of parts) {
      try {
        await bridge.call("split_recording", part.path, Object.assign(controls.value(), { keep_whole: keep.checked }));
        started++;
      } catch (error) {
        failed(error);
      }
    }
    modal.close();
    if (started) {
      showEditsNext();
      toast(t("Деление на части, ход виден в «Загрузках»"), "ok", "#downloads");
    }
  } });
  const skipped = partsOf(item).length - parts.length;
  modal.body.append(
    h("h2", { text: t("Разделить на части") }),
    h("div", { class: "muted field-gap", text: (parts.length > 1
      ? t("Частей эфира: {count}. Каждая поделится так, как выбрано.", { count: parts.length }) + " " : "")
      + t("Без перекодирования, по ключевым кадрам: части выходят примерно такими. Формат тот же, у каждой части свой чат.")
      + (skipped ? " " + t("Уже поделённые части ({count}) не трогаются.", { count: skipped }) : "") }),
    ...controls.fields,
    h("div", { class: "field" }, h("label", { class: "switch" }, keep, h("span", { class: "track" }),
      t("Оставить целую запись")), keepHint),
    h("div", { class: "actions" }, button(t("Отмена"), { onclick: () => modal.close() }), start));
}

// «Конвертировать запись»: сначала окно — во что, делить ли и как; сразу стоит то, что у канала
function openConvert(card) {
  const parts = partsOf(card._item).filter((part) => /\.ts$/i.test(part.path));
  if (!parts.length) return;
  const item = card._item;
  const controls = splitControls(item, true);
  const format = h("select", null, CONVERT_FORMATS.map(([value, label]) => h("option", { value: value, text: t(label) })));
  format.value = channelSetting(item, "convert_format") || "mp4";
  // кусок поделённой записи ещё раз не делится — только конвертируется; целая из whole
  // при конвертации тоже не делится (части легли бы в whole) — её делят ножницами
  const pieces = parts.every((part) => part.piece || part.whole);
  const modal = openModal();
  const start = button(t("Конвертировать"), { kind: "primary", onclick: () => {
    const split = controls.value();
    start.disabled = true;
    Promise.all(parts.map((part) => {
      state.converting.add(part.path);
      return bridge.call("convert_recording", part.path, split, format.value)
        .then(() => {
          showEditsNext();
          toast(t("Конвертация: {name}, ход виден в «Загрузках»", { name: part.name }), "ok", "#downloads");
        })
        .catch((error) => {
          state.converting.delete(part.path);
          throw error;
        });
    }))
      .then(() => modal.close())
      .catch((error) => {
        start.disabled = false;
        failed(error);
      });
  } });
  modal.body.append(
    h("h2", { text: t("Конвертировать запись") }),
    h("div", { class: "muted field-gap", text: pieces && parts.every((part) => part.whole)
      ? t("Целая запись перепишется в выбранный формат без перекодирования, чат — вместе с ней. Файл .ts после этого удалится. Разделить её — ножницами на карточке.")
      : pieces
      ? t("Части перепишутся в выбранный формат без перекодирования, каждая со своим чатом. Файлы .ts после этого удалятся.")
      : parts.length > 1
      ? t("Частей эфира в .ts: {count}. Каждая перепишется в выбранный формат и поделится так, как выбрано; чат — вместе с ней.", { count: parts.length })
      : t("Запись перепишется в выбранный формат без перекодирования и поделится так, как выбрано; у каждой части свой чат. Файл .ts после этого удалится.") }),
    field(t("Во что"), format),
    ...(pieces ? [] : controls.fields),
    h("div", { class: "actions" }, button(t("Отмена"), { onclick: () => modal.close() }), start));
}

// кадр карточки чата: значок и график, где чат писал больше всего
function drawChatThumb(thumb, bars) {
  const key = bars.join(",");
  if (thumb.dataset.bars === key) return;
  thumb.dataset.bars = key;
  const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
  svg.setAttribute("viewBox", "0 0 " + Math.max(1, bars.length) + " 100");
  svg.setAttribute("preserveAspectRatio", "none");
  const shape = document.createElementNS("http://www.w3.org/2000/svg", "path");
  shape.setAttribute("d", bars.map((value, index) => (value ? "M" + index + " 100V" + (100 - value) + "h0.8V100z" : "")).join(""));
  svg.append(shape);
  thumb.replaceChildren(icon("chat"), svg);
}

// файл только со звуком: кадра нет, видео чата на него не наложить
function isSound(path) {
  return /\.(m4a|mp3)$/i.test(path);
}

export function updateLibraryCard(card, item) {
  card._item = item;
  const parts = partsOf(item);
  // смотреть в окне можно любую запись: с чатом он идёт рядом, а клип вырезается и без него
  const withChat = parts.some((part) => part.chat && part.chat.json);
  card.title = withChat ? t("Смотреть с чатом") : t("Смотреть в окне");
  // чат без видео: смотреть и делить нечего — его открывают, сводка и поиск
  const onlyChat = item.video_kind === "chat";
  card._summary.title = onlyChat ? t("Сводка чата") : t("Сводка эфира");
  const placeholder = card.querySelector(".thumb .placeholder");
  if (placeholder && placeholder.dataset.kind !== (onlyChat ? "chat" : "video")) {
    placeholder.dataset.kind = onlyChat ? "chat" : "video";
    placeholder.replaceChildren(icon(onlyChat ? "chat" : "video"));
  }
  card.title = (onlyChat ? t("Читать чат: поиск, ники, всплески") : card.title)
    + "\n" + t("Правый щелчок — все действия");
  card._ts.hidden = !parts.some((part) => /\.ts$/i.test(part.path));
  card._parts.hidden = parts.length < 2;
  const gaps = parts.map((part) => part.gaps).filter(Boolean);
  const gapCount = gaps.reduce((sum, part) => sum + part.count, 0);
  const lost = gaps.reduce((sum, part) => sum + part.lost, 0);
  card._gaps.hidden = !gapCount;
  const join = parts.map((part) => state.joins.get(part.path)).find((event) => event && event.state !== "done");
  // не склеилось — меткой сверху; идёт — полосой по низу кадра с подписью на ней
  const joining = Boolean(join) && join.state === "joining";
  card._join.hidden = !join || joining;
  card._joinBar.hidden = !joining;
  card.classList.toggle("joining", joining);
  if (join && !joining) {
    card._join.className = "badge warn";
    setText(card._join, t("не склеено"));
    card._join.title = t("Начало эфира не склеено с записью: {reason}", { reason: join.reason || "?" });
  }
  if (joining) {
    const percent = join.waiting ? 100 : join.percent || 0;
    setText(card._joinText, join.waiting ? t("склеено, ожидание закрытия проигрывателя")
      : join.stage === "repack" ? t("подготовка к склейке…")
      : join.stage === "finish" ? t("завершение склейки…") : t("склеивается {percent}%", { percent: percent }));
    card._joinBar.title = joiningText(join);
    card._joinBar.firstChild.style.width = (join.stage ? 0 : percent) + "%";
    card._joinBar.classList.toggle("indeterminate", Boolean(join.stage));
  }
  setText(card._gaps, gapCount + " " + tp(gapCount, t("пропуск|пропуска|пропусков")));
  card._gaps.title = lost ? t("В записи пропущены отрезки эфира: пропало {length}", { length: fmt.duration(lost) })
                          : t("В записи пропущены отрезки эфира");
  setText(card._parts, parts.length + " " + tp(parts.length, t("часть|части|частей")));
  card._whole.hidden = !item.whole;
  setText(card._kind, item.edit === "chat" ? t("с чатом поверх")
                                            : t(VIDEO_KIND_LABELS[item.video_kind] || t("скачано")));
  setText(card._size, fmt.size(item.size));
  setText(card._title, displayTitle(item.title) || item.title);
  card._title.title = parts.map((part) => part.name).join("\n");
  fillChannelLine(card._sub, item.channel_id, channelName(item.channel_id, item.channel),
                  [platformName(item.platform), fmt.date(item.timestamp),
                   !item.parts && item.part ? t("часть {part}", { part: item.part }) : ""]);
  // чат без видео — не кадр и длительность эфира, а сам чат: график и число сообщений
  card._chatThumb.hidden = !onlyChat;
  if (onlyChat) {
    const facts = item.chat_facts || {};
    card._img.hidden = true;
    card._duration.hidden = !facts.messages;
    setText(card._duration, facts.messages ? messagesText(facts.messages) : "");
    card._duration.title = [facts.chatters ? fmt.counted(facts.chatters, t("зритель|зрителя|зрителей")) : "",
                            facts.length ? t("чат за {length}", { length: fmt.duration(facts.length) }) : ""]
      .filter(Boolean).join(" · ");
    drawChatThumb(card._chatThumb, facts.bars || []);
    return;
  }
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
  const targets = partsOf(item).filter((part) => part.chat && part.chat.video && !isSound(part.path));
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
    if (started) toast(t("Наложение чата на видео, ход виден в «Загрузках»"), "ok", "#downloads");
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
