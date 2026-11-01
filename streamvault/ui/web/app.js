/*
 * StreamVault: окно программы — точка входа.
 *
 * Страница общается с программой одним вызовом call(имя, аргументы):
 * в отдельном окне — через pywebview, в браузере — по HTTP с токеном.
 * Программа сама присылает события пачками в sv.receive().
 *
 * Окно разбито на ES-модули без сборщика (CSP script-src 'self' их
 * разрешает): common — общее, channels — каналы и их записи, downloads,
 * library, player — просмотр с чатом, settings, search. Здесь — события
 * программы, маршруты, журнал и запуск.
 */

import { START_WAIT, bridge, button, ensureProfiles, failed, fmt, h, icon, openModal, rememberProfile, setText, showStopped, state, t, toast } from "./common.js";
import { refreshSidebar, renderChannel, renderLink, renderLive } from "./channels.js";
import { refreshDownloadsCount, renderDownloads } from "./downloads.js";
import { renderLibrary, scheduleLibraryReload } from "./library.js";
import { ffmpegBlock, openAppUpdate, openSettings, refreshAppUpdate, refreshFfmpegBlocks, refreshTwitchdownloaderBlocks, refreshYtdlpBlocks, setAppUpdate } from "./settings.js";
import { attachChannelSearch, openAddChannel } from "./search.js";
import { enhanceSelects } from "./dropdown.js";

export function receive(batch) {
  let channelsChanged = false, jobsChanged = false;
  for (const event of batch) {
    switch (event.type) {
      case "channel":
        state.channels.set(event.channel.id, event.channel);
        rememberProfile(event.channel);
        channelsChanged = true;
        break;
      case "channel_removed":
        state.channels.delete(event.id);
        channelsChanged = true;
        break;
      case "download":
        noticeJob(event.job);
        state.jobs.set(event.job.id, event.job);
        jobsChanged = true;
        break;
      case "edit":
        noticeEdit(event.job);
        state.edits.set(event.job.id, event.job);
        jobsChanged = true;
        break;
      case "joining":
        noticeJoin(event);
        state.joins.set(event.head, event);
        jobsChanged = true;
        break;
      case "download_removed":
        state.jobs.delete(event.id);
        jobsChanged = true;
        break;
      case "processing":
        scheduleLiveResync();
        // запись сконвертирована — в библиотеке появился новый файл
        if (event.action === "done") scheduleLibraryReload();
        // ручная конвертация не удалась — кнопка снова доступна
        if (event.action === "gave_up" && state.converting.delete(event.file)) scheduleLibraryReload();
        if (event.action === "done") state.converting.delete(event.file);
        break;
      case "recording":
        updateRecordingBytes(event.bytes, event.chats || {});
        break;
      case "log":
        appendLog(event);
        break;
      case "ffmpeg":
        state.ffmpeg = Object.assign({}, event.status, { install: event.install });
        refreshFfmpegBlocks();
        break;
      case "ytdlp":
        state.ytdlp = Object.assign({}, state.ytdlp, event.status, { install: event.install });
        refreshYtdlpBlocks();
        break;
      case "twitchdownloader":
        state.twitchdownloader = Object.assign({}, event.status, { install: event.install });
        refreshTwitchdownloaderBlocks();
        break;
      case "app_update":
        setAppUpdate(event.install);
        break;
      case "disk":
        state.disk = event;
        refreshDiskBanner();
        break;
      case "integrity":
        // проверка записи закончилась: сводка и проигрыватель слушают это сами
        window.dispatchEvent(new CustomEvent("integrity", { detail: event }));
        scheduleLibraryReload();
        break;
    }
  }
  if (channelsChanged) {
    refreshSidebar();
    if (state.route.name === "live" || state.route.name === "channel") refreshView();
    ensureProfiles([...state.channels.keys()]);
  }
  if (jobsChanged) {
    refreshDownloadsCount();
    if (state.route.name === "downloads" || state.route.name === "channel"
        || state.route.name === "link") refreshView();
  }
}

// уведомления о том, что загрузка закончилась
// правка готова или не удалась — сказать, а готовое показать в библиотеке
export function noticeEdit(job) {
  const before = state.edits.get(job.id);
  if (before && before.state === job.state) return;
  if (job.state === "done") {
    toast(t("Готово: {name}", { name: job.name }), "ok", "#library");
    scheduleLibraryReload();
  } else if (job.state === "failed") {
    toast(t("Не получилось: {name} — {error}", { name: job.name, error: job.error }), "error");
  }
}

// начало эфира склеено с записью или не склеилось — сказать; склеенное — в библиотеке
export function noticeJoin(event) {
  const before = state.joins.get(event.head);
  if (before && before.state === event.state) return;
  const name = event.main.split(/[\\/]/).pop();
  if (event.state === "done") {
    toast(t("Начало эфира склеено с записью: {name}", { name: name }), "ok", "#library");
    scheduleLibraryReload();
  } else if (event.state === "failed") {
    toast(t("Начало эфира не склеено с записью: {reason}", { reason: event.reason }), "error");
  }
}

export function noticeJob(job) {
  const before = state.jobs.get(job.id);
  if (!before || before.state === job.state) return;
  if (job.state === "done") scheduleLibraryReload();
  if (job.state === "done") toast(t("Скачано: {name}", { name: job.title || job.url }), "ok");
  else if (job.state === "failed") {
    toast(t("Не скачалось: {name} — {error}", { name: job.title || job.url, error: job.error }), "error");
  }
}

export function updateRecordingBytes(bytes, chats) {
  for (const [id, size] of Object.entries(bytes)) {
    const channel = state.channels.get(id);
    if (channel && channel.recording) {
      channel.recording.bytes = size;
      if (chats[id] !== undefined) channel.recording.chat_messages = chats[id];
    }
  }
  for (const el of document.querySelectorAll("[data-rec-id]")) {
    const size = bytes[el.dataset.recId];
    if (size !== undefined) el.dataset.bytes = size;
    if (chats[el.dataset.recId] !== undefined) el.dataset.chat = chats[el.dataset.recId];
  }
  tick();
}

export let liveResyncTimer = null;
export function scheduleLiveResync() {
  clearTimeout(liveResyncTimer);
  liveResyncTimer = setTimeout(async () => {
    try {
      applyLive(await bridge.call("live_snapshot"));
      refreshSidebar();
      if (state.route.name === "live" || state.route.name === "channel") refreshView();
    } catch (error) { /* следующее событие попробует снова */ }
  }, 700);
}

export function applyLive(live) {
  state.channels = new Map(live.channels.map((channel) => [channel.id, channel]));
  live.channels.forEach(rememberProfile);
  state.processing = live.processing;
}

// длительность идущих записей и "проверено N минут назад" обновляются сами
export function tick() {
  const now = Date.now() / 1000;
  // запись так и не пошла за отведённое время — кнопка возвращается к «Начать запись»
  if (state.recordStarting.size && [...state.recordStarting.values()].some((since) => Date.now() - since > START_WAIT)) {
    refreshView();
  }
  for (const el of document.querySelectorAll("[data-since]")) {
    const label = "REC " + fmt.duration(now - Number(el.dataset.since))
                  + (el.dataset.bytes ? " · " + fmt.size(Number(el.dataset.bytes)) : "")
                  + (el.dataset.chat ? " · " + t("чат {count}", { count: el.dataset.chat }) : "");
    setText(el.lastChild && el.lastChild.nodeType === 3 ? el.lastChild : el, label);
  }
  for (const el of document.querySelectorAll("[data-ago]")) {
    setText(el, (el.dataset.prefix || "") + fmt.ago(Number(el.dataset.ago)));
  }
}

export function recBadge(channel) {
  const badge = h("span", { class: "badge rec" }, "");
  badge.dataset.since = channel.recording.started;
  badge.dataset.bytes = channel.recording.bytes || 0;
  // сколько сообщений чата эфира записано (если чат пишется)
  if (channel.recording.chat_messages !== null && channel.recording.chat_messages !== undefined) {
    badge.dataset.chat = channel.recording.chat_messages;
  }
  badge.dataset.recId = channel.id;
  return badge;
}

export function parseRoute() {
  let hash = location.hash.slice(1);
  try { hash = decodeURIComponent(hash); } catch (error) { /* ссылка с одиночным % */ }
  const slash = hash.indexOf("/");
  const name = slash < 0 ? hash : hash.slice(0, slash);
  const arg = slash < 0 ? "" : hash.slice(slash + 1);
  if (name === "channel" && arg) return { name: "channel", id: arg };
  if (name === "link" && arg) return { name: "link", url: arg };
  if (name === "downloads" || name === "log" || name === "library") return { name: name };
  return { name: "live" };
}

export function routeHash(name, arg) {
  return "#" + name + (arg ? "/" + encodeURIComponent(arg) : "");
}

export function navigate(hash) {
  if (location.hash === hash) render();
  else location.hash = hash;
}

export let view = {};

export function render() {
  // раздел сменили, пока программа ещё не ответила на init: нарисуется после него
  if (!state.info) return;
  state.route = parseRoute();
  for (const link of document.querySelectorAll("#nav a")) {
    link.classList.toggle("active", link.dataset.view === state.route.name);
  }
  const main = document.getElementById("main");
  main.replaceChildren();
  main.scrollTop = 0;
  view = { route: state.route };
  if (state.problems.length) main.append(problemsBanner());
  main.append(diskBanner);
  refreshDiskBanner();
  if (state.ffmpeg && !state.ffmpeg.path) main.append(ffmpegBlock(true));
  switch (state.route.name) {
    case "channel": renderChannel(main, state.route.id); break;
    case "link": renderLink(main, state.route.url); break;
    case "downloads": renderDownloads(main); break;
    case "library": renderLibrary(main); break;
    case "log": renderLog(main); break;
    default: renderLive(main);
  }
  refreshSidebar();
  tick();
}

export function refreshView() {
  if (view.refresh) view.refresh();
  tick();
}

// место на диске с записями: плашка, пока его мало
export const diskBanner = h("div", { class: "banner" });
diskBanner.hidden = true;

export function refreshDiskBanner() {
  const disk = state.disk || {};
  diskBanner.hidden = !["low", "critical"].includes(disk.level);
  if (diskBanner.hidden) return;
  const free = fmt.size(disk.free || 0);
  diskBanner.classList.toggle("warn", disk.level === "low");
  diskBanner.replaceChildren(icon("alert"), h("div", { text: disk.level === "critical"
    ? t("На диске с записями осталось {free} — запись остановлена, пока место не освободится.", { free: free })
    : t("На диске с записями осталось {free}. Освободи место, иначе запись остановится.", { free: free }) }));
}

export function problemsBanner() {
  return h("div", { class: "banner" }, icon("alert"),
           h("div", null, state.problems.map((problem) => h("div", { text: problem }))));
}

export const LOG_LIMIT = 1000;

export function renderLog(main) {
  view.filter = h("select", null,
    h("option", { value: "info", text: t("Все сообщения") }),
    h("option", { value: "warning", text: t("Предупреждения и ошибки") }),
    h("option", { value: "error", text: t("Только ошибки") }));
  view.filter.value = sessionStorage.getItem("log-filter") || "info";
  view.filter.addEventListener("change", () => {
    sessionStorage.setItem("log-filter", view.filter.value);
    fillLog();
  });
  view.log = h("div", { class: "log" });
  main.append(h("div", { class: "toolbar" },
    h("h1", { class: "page-title", text: t("Журнал") }), h("span", { class: "spacer" }), view.filter,
    button(t("Папка логов"), { icon: "folder", onclick: () => bridge.call("open_folder", "logs").catch(failed) })),
    view.log);
  fillLog();
}

export const LEVELS = { debug: 0, info: 1, warning: 2, error: 3, critical: 4 };

export function logVisible(entry) {
  return (LEVELS[entry.level] || 0) >= (LEVELS[view.filter.value] || 1);
}

export function logLine(entry) {
  return h("div", { class: "log-line " + entry.level },
    h("span", { class: "time", text: fmt.clock(entry.created || entry.time) }),
    h("span", { text: entry.message }));
}

export function fillLog() {
  view.log.replaceChildren(...state.logs.filter(logVisible).map(logLine));
  view.log.scrollTop = view.log.scrollHeight;
}

export function appendLog(entry) {
  // строка, пришедшая и в первом снимке, и событием (окно открылось посреди потока), — одна
  const recent = state.logs.slice(-200);
  if (recent.some((known) => known.created === entry.created && known.message === entry.message)) return;
  state.logs.push(entry);
  if (state.logs.length > LOG_LIMIT) state.logs.splice(0, state.logs.length - LOG_LIMIT);
  if (state.route.name !== "log" || !view.log || !logVisible(entry)) return;
  const atBottom = view.log.scrollHeight - view.log.scrollTop - view.log.clientHeight < 40;
  view.log.append(logLine(entry));
  while (view.log.children.length > LOG_LIMIT) view.log.firstChild.remove();
  if (atBottom) view.log.scrollTop = view.log.scrollHeight;
}

export function askQuit(always) {
  const modal = openModal();
  modal.body.append(
    h("h2", { text: t("Выйти из StreamVault?") }),
    h("div", { class: "muted", text: always
      ? t("Записи и загрузки остановятся. Записанное сохранится, недокачанное продолжится при следующем запуске.")
      : t("Сейчас идёт запись или загрузка. Записанное сохранится, недокачанное продолжится при следующем запуске.") }),
    h("div", { class: "actions" },
      button(t("Отмена"), { onclick: () => modal.close() }),
      button(t("Выйти"), { kind: "primary danger", onclick: () => {
        modal.close();
        state.stopped = true;
        bridge.call("quit").catch(() => {});
        if (bridge.browserMode) showStopped(t("StreamVault завершён. Эту вкладку можно закрыть."));
      } })));
}

export function setupChrome() {
  document.querySelector(".search-button").append(icon("search"));
  document.getElementById("settings-button").append(icon("tune"));
  document.getElementById("add-channel-button").append(icon("plus"));
  // крестик окна сворачивает программу в трей, а выйти — здесь
  const quit = document.getElementById("quit-button");
  quit.append(icon("power"));
  quit.addEventListener("click", () => askQuit(true));
  document.getElementById("settings-button").addEventListener("click", openSettings);
  document.getElementById("update-badge").addEventListener("click", openAppUpdate);
  document.getElementById("add-channel-button").addEventListener("click", openAddChannel);
  document.getElementById("sidebar-add").addEventListener("click", openAddChannel);

  const input = document.getElementById("search-input");
  const results = attachChannelSearch(input, (channel) => {
    input.value = "";
    input.blur();
    navigate(routeHash("channel", channel.id));
  });
  document.getElementById("search-form").addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = input.value.trim();
    if (!text) return;
    try {
      const target = await bridge.call("resolve", text);
      input.value = "";
      results.clear();
      input.blur();
      navigate(target.kind === "channel" ? routeHash("channel", target.id) : routeHash("link", target.url));
    } catch (error) {
      failed(error);
    }
  });
  window.addEventListener("hashchange", render);
}

export async function start() {
  setupChrome();
  await bridge.ready();
  if (bridge.browserMode) await bridge.mark();
  let data;
  try {
    data = await bridge.call("init");
  } catch (error) {
    showStopped(error.message);
    return;
  }
  state.info = data;
  window.I18N.setLanguage(data.language);
  window.I18N.applyStatic(document);
  state.ffmpeg = data.ffmpeg || {};
  state.ytdlp = data.ytdlp || {};
  state.twitchdownloader = data.twitchdownloader || {};
  state.appUpdate = data.app_update || {};
  refreshAppUpdate();
  if (data.last_update) {
    // чем кончилось обновление перед этим запуском
    if (data.last_update.ok) toast(t("StreamVault обновлён до {version}", { version: data.last_update.version }), "ok");
    else toast(t("Обновиться не получилось: {error}", { error: data.last_update.error || "?" }), "error");
  }
  applyLive(data.live);
  state.jobs = new Map(data.downloads.jobs.map((job) => [job.id, job]));
  state.joins = new Map((data.joins || []).map((event) => [event.head, event]));
  state.settings = data.settings;
  state.logs = data.logs || [];
  state.problems = data.problems || [];
  state.disk = data.disk || {};
  state.edits = new Map((data.edits || []).map((job) => [job.id, job]));
  refreshDownloadsCount();
  render();
  ensureProfiles([...state.channels.keys()]);
  if (bridge.browserMode) bridge.poll();
  setInterval(tick, 1000);
  // раз в полминуты сверяемся целиком: "проверено N минут назад" и порядок каналов
  setInterval(scheduleLiveResync, 30000);
}

// выпадающие списки — свои, в тон окну (dropdown.js)
enhanceSelects(document.body);
window.sv = { receive: receive, askQuit: () => askQuit(false) };
start();
