/*
 * StreamVault, окно программы: общее для всех частей окна — разметка,
 * значки, форматирование, связь с программой, состояние и окна поверх.
 *
 * Названия стримов, имена каналов и прочее с чужих сайтов вставляются
 * только как текст (textContent), никогда как разметка: иначе название
 * стрима могло бы выполнить скрипт в окне программы.
 */

import { refreshSidebar } from "./channels.js";
import { receive, refreshView, view } from "./app.js";

export const { t, tp } = window.I18N;

export function h(tag, attrs) {
  const el = document.createElement(tag);
  if (attrs) {
    for (const [key, value] of Object.entries(attrs)) {
      if (value === null || value === undefined || value === false) continue;
      if (key === "class") el.className = value;
      else if (key === "text") el.textContent = value;
      else if (key.startsWith("on")) el.addEventListener(key.slice(2), value);
      else el.setAttribute(key, value === true ? "" : value);
    }
  }
  for (let i = 2; i < arguments.length; i++) append(el, arguments[i]);
  return el;
}

export function append(el, child) {
  if (child === null || child === undefined || child === false) return;
  if (Array.isArray(child)) { child.forEach((item) => append(el, item)); return; }
  el.appendChild(typeof child === "object" ? child : document.createTextNode(String(child)));
}

export function setText(el, text) {
  text = text === null || text === undefined ? "" : String(text);
  if (el.textContent !== text) el.textContent = text;
}

// картинки — только с https (превью и аватарки с сайтов платформ)
// и кадры, которые сама программа достала из записей
export function safeUrl(url) {
  if (typeof url !== "string") return "";
  return /^https:\/\//i.test(url) || /^data:image\/(jpeg|png);base64,/.test(url) ? url : "";
}

export function setSrc(img, url) {
  url = safeUrl(url);
  if ((img.getAttribute("src") || "") === url) return;
  if (url) img.src = url; else img.removeAttribute("src");
  img.hidden = !url;
}

/*
 * Список с ключами: существующие элементы обновляются и переставляются,
 * а не создаются заново — иначе превью мигали бы при каждом событии.
 */
export function syncList(container, items, keyOf, create, update) {
  const existing = new Map();
  for (const el of container.children) existing.set(el.dataset.key, el);
  let previous = null;
  for (const item of items) {
    const key = String(keyOf(item));
    let el = existing.get(key);
    if (el) existing.delete(key);
    else { el = create(item); el.dataset.key = key; }
    update(el, item);
    const expected = previous ? previous.nextSibling : container.firstChild;
    if (el !== expected) container.insertBefore(el, expected);
    previous = el;
  }
  existing.forEach((el) => el.remove());
}

// Значки: постоянная разметка из этого файла, данные в неё не попадают.
export const ICONS = {
  plus: "M11 5h2v14h-2zM5 11h14v2H5z",
  tune: "M3 6h11v2H3zm14 0h4v2h-4zm-2-2h2v6h-2zM3 16h5v2H3zm8 0h10v2H11zm-2-2h2v6H9z",
  search: "M10 4a6 6 0 104.47 10.03l4.82 4.82 1.41-1.41-4.82-4.82A6 6 0 0010 4zm0 2a4 4 0 110 8 4 4 0 010-8z",
  power: "M11 3h2v9h-2zM7.05 6.05l1.41 1.41a6 6 0 107.08 0l1.41-1.41a8 8 0 11-9.9 0z",
  play: "M8 5v14l11-7z",
  volume: "M4 9h4l5-4v14l-5-4H4zm12.5-1.1a5 5 0 010 8.2l-1.2-1.6a3 3 0 000-5z",
  muted: "M4 9h4l5-4v14l-5-4H4zm11.6.6L17 8.2l2 2 2-2 1.4 1.4-2 2 2 2-1.4 1.4-2-2-2 2-1.4-1.4 2-2z",
  fullscreen: "M4 4h6v2H6v4H4zm10 0h6v6h-2V6h-4zM4 14h2v4h4v2H4zm14 0h2v6h-6v-2h4z",
  pause: "M7 5h4v14H7zm6 0h4v14h-4z",
  close: "M6.4 5L5 6.4 10.6 12 5 17.6 6.4 19 12 13.4 17.6 19 19 17.6 13.4 12 19 6.4 17.6 5 12 10.6z",
  trash: "M9 3h6l1 2h4v2H4V5h4zM6 9h12l-1 12H7z",
  folder: "M3 6a2 2 0 012-2h4l2 2h8a2 2 0 012 2v10a2 2 0 01-2 2H5a2 2 0 01-2-2z",
  external: "M14 4h6v6h-2V7.41l-7.29 7.3-1.42-1.42L16.59 6H14zM5 6h6v2H7v9h9v-4h2v6H5z",
  refresh: "M12 5V2L8 6l4 4V7a5 5 0 11-5 5H5a7 7 0 107-7z",
  download: "M11 4h2v8.17l3.59-3.58L18 10l-6 6-6-6 1.41-1.41L11 12.17zM5 18h14v2H5z",
  alert: "M12 2L1 21h22zm-1 7h2v6h-2zm0 8h2v2h-2z",
  video: "M4 6h12v12H4zm13 4l4-3v10l-4-3z",
  chat: "M4 3h16a1 1 0 011 1v11a1 1 0 01-1 1H9.4L5 19.5V16H4a1 1 0 01-1-1V4a1 1 0 011-1zm1 2v9h2v1.4L8.6 14H19V5zm2 2h10v2H7zm0 3h7v2H7z",
  file: "M6 2h8l6 6v14H6zm7 1.5V9h5.5z",
  check: "M9 16.2l-4.2-4.2-1.4 1.4L9 19 21 7l-1.4-1.4z",
  // стрелка выпадающего списка
  chevron: "M7.41 8.59L12 13.17l4.59-4.58L18 10l-6 6-6-6z",
  record: "M12 5a7 7 0 100 14 7 7 0 000-14z",
  stop: "M6 6h12v12H6z",
  convert: "M4 7h12.17l-3.58-3.59L14 2l6 6-6 6-1.41-1.41L16.17 9H4zm16 10H7.83l3.58 3.59L10 22l-6-6 " +
           "6-6 1.41 1.41L7.83 15H20z",
  // ножницы: вырезать клип
  scissors: "M6 3a3 3 0 110 6 3 3 0 010-6zm0 2a1 1 0 100 2 1 1 0 000-2zM6 15a3 3 0 110 6 3 3 0 010-6zm0 " +
            "2a1 1 0 100 2 1 1 0 000-2zM8.3 7.7L20 18.6V20h-1.4L7.6 9.1zM20 4v1.4L8.3 16.3l-.7-1.4L18.6 4z",
  // кадр с колонкой чата сбоку: видео с чатом поверх
  overlay: "M3 5h18v14H3zm2 2v10h9V7zm11 0v10h3V7z",
  // проигрыватель как на Twitch: шестерёнка настроек, свернуть и развернуть чат, кинотеатр
  gear: "M19.14 12.94a7.1 7.1 0 000-1.88l2.03-1.58a.5.5 0 00.12-.61l-1.92-3.32a.5.5 0 00-.59-.22l-2.39.96" +
        "a7 7 0 00-1.62-.94l-.36-2.54A.48.48 0 0013.93 2h-3.84a.47.47 0 00-.47.41l-.36 2.54a7.3 7.3 0 00-1.62.94" +
        "l-2.39-.96a.48.48 0 00-.59.22L2.74 8.47a.47.47 0 00.12.61l2.03 1.58a7.3 7.3 0 000 1.88l-2.03 1.58" +
        "a.5.5 0 00-.12.61l1.92 3.32a.5.5 0 00.59.22l2.39-.96c.5.38 1.03.7 1.62.94l.36 2.54c.05.24.24.41.48.41" +
        "h3.84c.24 0 .44-.17.47-.41l.36-2.54a7 7 0 001.62-.94l2.39.96a.48.48 0 00.59-.22l1.92-3.32a.48.48 0 00" +
        "-.12-.61zM12 15.6a3.6 3.6 0 110-7.2 3.6 3.6 0 010 7.2z",
  collapse: "M4 4h2v16H4zm4 7h9.17l-3.58-3.59L15 6l6 6-6 6-1.41-1.41L17.17 13H8z",
  expand: "M18 4h2v16h-2zm-2 7H6.83l3.58-3.59L9 6l-6 6 6 6 1.41-1.41L6.83 13H16z",
  theatre: "M2 6h20v12H2zm2 2v8h16V8z",
  // столбики: сводка эфира
  stats: "M4 19h16v2H4zM5 10h3v7H5zm5.5-6h3v13h-3zM16 13h3v4h-3z",
};

export function icon(name) {
  const span = document.createElement("span");
  span.className = "icon";
  span.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="' + ICONS[name] + '"/></svg>';
  return span;
}

export function button(label, options) {
  options = options || {};
  const el = h("button", {
    class: "btn" + (options.kind ? " " + options.kind : ""),
    type: options.type || "button",
    title: options.title,
    onclick: options.onclick,
  }, options.icon ? icon(options.icon) : null, label);
  if (options.disabled) el.disabled = true;
  return el;
}

export function iconButton(name, title, onclick) {
  // title — подсказка при наведении, aria-label — имя кнопки для экранного диктора
  return h("button", { class: "icon-button", type: "button", title: title, "aria-label": title, onclick: onclick },
           icon(name));
}

/*
 * Значок «?» с подсказкой: открывается при наведении, а по щелчку
 * остаётся открытой, пока не щёлкнуть мимо. build() — содержимое.
 */
export function helpTip(build) {
  const tip = h("span", { class: "help-tip" });
  const mark = h("button", { class: "help-mark", type: "button", text: "?", "aria-label": t("Подсказка") });
  const pop = h("div", { class: "help-pop", role: "tooltip" }, ...build());
  let timer = null;
  const show = () => {
    clearTimeout(timer);
    const place = tip.getBoundingClientRect();
    // у правого края окна подсказка раскрывается влево, чтобы не уйти за край
    pop.classList.toggle("to-left", place.left > window.innerWidth / 2);
    pop.classList.remove("to-top");
    tip.classList.add("open");
    // снизу не помещается, а сверху места больше — вверх
    const height = pop.getBoundingClientRect().height;
    if (place.bottom + height + 12 > window.innerHeight && place.top > window.innerHeight - place.bottom) {
      pop.classList.add("to-top");
    }
  };
  tip.addEventListener("mouseenter", show);
  tip.addEventListener("mouseleave", () => {
    timer = setTimeout(() => { if (!tip.classList.contains("pinned")) tip.classList.remove("open"); }, 250);
  });
  mark.addEventListener("click", (event) => {
    event.preventDefault();
    const pinned = !tip.classList.contains("pinned");
    if (pinned) show();
    tip.classList.toggle("pinned", pinned);
    tip.classList.toggle("open", pinned);
  });
  tip.append(mark, pop);
  return tip;
}

document.addEventListener("click", (event) => {
  for (const tip of document.querySelectorAll(".help-tip.pinned")) {
    if (!tip.contains(event.target)) tip.classList.remove("pinned", "open");
  }
});

export function pad(n) { return n < 10 ? "0" + n : String(n); }

export function english() { return window.I18N.language === "en"; }

export const fmt = {
  size(bytes) {
    if (bytes === null || bytes === undefined) return "?";
    const units = english() ? ["B", "KB", "MB", "GB", "TB"] : ["Б", "КБ", "МБ", "ГБ", "ТБ"];
    let value = bytes, unit = 0;
    while (value >= 1024 && unit < units.length - 1) { value /= 1024; unit++; }
    const text = unit === 0 ? String(value) : value.toFixed(value < 10 ? 1 : 0);
    return (english() ? text : text.replace(".", ",")) + " " + units[unit];
  },
  duration(seconds) {
    if (seconds === null || seconds === undefined) return "";
    seconds = Math.max(0, Math.floor(seconds));
    const hours = Math.floor(seconds / 3600), minutes = Math.floor(seconds % 3600 / 60);
    return hours ? hours + ":" + pad(minutes) + ":" + pad(seconds % 60)
                 : minutes + ":" + pad(seconds % 60);
  },
  count(n) {
    if (n === null || n === undefined) return "";
    if (n < 1000) return String(n);
    // 1,7 тыс. и 1.7K
    const [value, unit] = n < 1e6 ? [n / 1000, english() ? "K" : " тыс."]
                                  : [n / 1e6, english() ? "M" : " млн"];
    const text = value.toFixed(value < 10 ? 1 : 0).replace(/\.0$/, "");
    return (english() ? text : text.replace(".", ",")) + unit;
  },
  // "1,7 тыс. зрителей": после "тыс." по-русски всегда родительный падеж
  counted(n, forms) {
    const word = n >= 1000 && !english() ? forms.split("|")[2] : tp(n, forms);
    return fmt.count(n) + " " + word;
  },
  viewers(n) { return fmt.counted(n, t("зритель|зрителя|зрителей")); },
  views(n) { return fmt.counted(n, t("просмотр|просмотра|просмотров")); },
  followers(n) { return fmt.counted(n, t("подписчик|подписчика|подписчиков")); },
  date(ts) {
    if (!ts) return "";
    return new Date(ts * 1000).toLocaleDateString(english() ? "en-US" : "ru-RU",
                                                  { day: "numeric", month: "short", year: "numeric" });
  },
  ago(ts) {
    if (!ts) return "";
    const seconds = Math.max(0, Date.now() / 1000 - ts);
    if (seconds < 45) return t("только что");
    const ago = (n, forms) => t("{n} {unit} назад", { n: n, unit: tp(n, forms) });
    if (seconds < 3600) return ago(Math.round(seconds / 60), t("минуту|минуты|минут"));
    if (seconds < 86400) return ago(Math.round(seconds / 3600), t("час|часа|часов"));
    if (seconds < 30 * 86400) return ago(Math.round(seconds / 86400), t("день|дня|дней"));
    return fmt.date(ts);
  },
  clock(ts) {
    const d = new Date(ts * 1000);
    return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds());
  },
};

export const bridge = {
  token: new URLSearchParams(location.search).get("token"),

  get browserMode() { return Boolean(this.token); },

  ready() {
    if (this.browserMode) return Promise.resolve();
    return new Promise((resolve) => {
      if (window.pywebview && window.pywebview.api && window.pywebview.api.call) resolve();
      else window.addEventListener("pywebviewready", () => resolve(), { once: true });
    });
  },

  async call(name, ...args) {
    let reply;
    if (this.browserMode) {
      const response = await fetch("/api", {
        method: "POST",
        headers: { "Content-Type": "application/json", "X-StreamVault-Token": this.token },
        body: JSON.stringify({ name: name, args: args }),
      });
      reply = await response.json();
    } else {
      reply = await window.pywebview.api.call(name, args);
    }
    if (!reply || !reply.ok) throw new Error((reply && reply.error) || t("программа не ответила"));
    return reply.result;
  },

  // номер последнего события до снимка состояния: после снимка нужны только новые
  async mark() {
    try {
      const response = await fetch("/events?after=-1", { headers: { "X-StreamVault-Token": this.token } });
      this.after = (await response.json()).seq || 0;
    } catch (error) {
      this.after = 0;
    }
  },

  // в браузере события забираются долгими запросами
  async poll() {
    let after = this.after || 0, failures = 0;
    while (!state.stopped) {
      try {
        const response = await fetch("/events?after=" + after,
                                     { headers: { "X-StreamVault-Token": this.token } });
        if (!response.ok) throw new Error(String(response.status));
        const data = await response.json();
        after = data.seq;
        failures = 0;
        data.batches.forEach(receive);
      } catch (error) {
        if (++failures >= 3) { showStopped(t("Нет связи с программой. Возможно, она закрыта.")); return; }
        await new Promise((resolve) => setTimeout(resolve, 2000));
      }
    }
  },
};

export function failed(error) {
  toast(error && error.message ? error.message : String(error), "error");
}

export const state = {
  info: null,
  channels: new Map(),
  processing: { pending: 0, current: null },
  jobs: new Map(),
  logs: [],
  settings: {},
  profiles: new Map(),
  problems: [],
  listings: new Map(),
  edits: new Map(),        // правки записей: клипы и видео с чатом поверх
  converting: new Set(),   // записи .ts, которые конвертируются по кнопке в библиотеке
  recordBusy: new Set(),   // каналы, которым только что нажали «Записать» или «Остановить»
  recordStarting: new Map(), // «Записать» нажали, запись ещё не пошла: id канала → когда нажали
  route:{ name: "live" },
  stopped: false,
};

export const PLATFORM = {
  twitch: { name: "Twitch", color: "#9147ff" },
  kick: { name: "Kick", color: "#2bb24c" },
  youtube: { name: "YouTube", color: "#e62117" },
  vkvideolive: { name: "VK Video Live", color: "#0077ff" },
};

export const KIND_LABELS = {
  vods: "Прошедшие эфиры", highlights: "Хайлайты", uploads: "Загруженное",
  clips: "Клипы", videos: "Видео", shorts: "Shorts",
};

export const PERIOD_LABELS = { "24hr": "Популярные за сутки", "7d": "Популярные за неделю",
                        "30d": "Популярные за месяц", all: "Популярные за всё время" };

// качество записи эфира — как его называет streamlink
export const LIVE_QUALITIES = ["best", "1080p60", "1080p", "720p60", "720p", "480p", "360p", "160p", "worst", "audio_only"];
export const LIVE_QUALITY_NOTES = { best: "лучшее", worst: "худшее", audio_only: "только звук" };

// качество записи по-человечески: «лучшее», а не «best — лучшее»
export function liveQualityLabel(quality) {
  return LIVE_QUALITY_NOTES[quality] ? t(LIVE_QUALITY_NOTES[quality]) : quality || "";
}

// сколько дней хранить записи канала; пусто — как в настройках
export const KEEP_DAYS = [0, 7, 14, 30, 60, 90, 180, 365];

export function keepDaysLabel(days) {
  if (days === null || days === undefined) {
    const general = state.settings.keep_days || 0;
    return t("как в настройках ({keep})", { keep: general ? general + " " + tp(general, "день|дня|дней") : t("всегда") });
  }
  return days ? t("хранить {keep}", { keep: days + " " + tp(days, "день|дня|дней") }) : t("хранить всегда");
}

export function setKeepDays(select, days) {
  const value = days === null || days === undefined ? "" : String(days);
  if (![...select.options].some((option) => option.value === value)) {
    select.append(h("option", { value: value, text: keepDaysLabel(days) }));
  }
  select.value = value;
}

export function keepDaysSelect(days) {
  const select = h("select", { title: t("Сколько дней хранить записи канала: старые уходят в корзину вместе с чатом") },
    h("option", { value: "", text: keepDaysLabel(null) }),
    KEEP_DAYS.map((value) => h("option", { value: String(value), text: keepDaysLabel(value) })));
  setKeepDays(select, days);
  return select;
}

// чат записей: Twitch, Kick, VK Video Live и прошедшие эфиры YouTube
export const CHAT_MODES = [["none", "без чата"], ["text", "чат для браузера"], ["render", "чат + видео чата"]];
export const CHAT_STAGES = { download: "скачиваю чат", html: "готовлю чат для браузера", render: "рисую видео чата" };

// что умеет площадка, решает программа (sites в init), а не окно
export function siteInfo(platform) { return ((state.info && state.info.sites) || {})[platform] || null; }

export function chatSupported(item) {
  const site = siteInfo(item.platform);
  return Boolean(site) && site.chat_kinds.includes(item.kind)
    && !site.chat_skip_status.includes(item.live_status || "");
}

export function chatSelect(value) {
  const select = h("select", { title: t("Чат: для браузера (HTML) и по желанию видео чата") });
  for (const [mode, label] of CHAT_MODES) select.append(h("option", { value: mode, text: t(label) }));
  select.value = value || state.settings.chat_mode || "none";
  return select;
}

/*
 * Качество записи — выпадающим списком. Раньше было поле с подсказками,
 * и браузер показывал в них только похожее на уже введённое: выбрав
 * "best", другое качество было уже не выбрать. inherit — подпись пустого
 * варианта ("как в настройках"), если он нужен.
 */
export function liveQualitySelect(value, inherit) {
  const select = h("select", { title: t("Качество записи") });
  // «как в настройках» — и какое там стоит
  if (inherit) {
    const inherited = state.settings && state.settings.quality;
    select.append(h("option", { value: "", text: inherited ? inherit + " (" + liveQualityLabel(inherited) + ")" : inherit }));
  }
  for (const quality of LIVE_QUALITIES) select.append(h("option", { value: quality, text: liveQualityLabel(quality) }));
  setLiveQuality(select, value);
  return select;
}

export function setLiveQuality(select, value) {
  value = value || "";
  // своё значение, вписанное в settings.json руками, тоже должно показываться
  if (value && ![...select.options].some((option) => option.value === value)) {
    select.append(h("option", { value: value, text: value }));
  }
  select.value = value;
  if (select.value !== value) select.selectedIndex = 0;
}
export const DOWNLOAD_QUALITIES = [
  { quality: "best", label: "лучшее" }, { quality: "1080", label: "1080p" },
  { quality: "720", label: "720p" }, { quality: "480", label: "480p" },
  { quality: "360", label: "360p" }, { quality: "worst", label: "худшее" },
  { quality: "audio", label: "только звук" },
];

export const PAGE = 24;

export function platformName(platform) { return (PLATFORM[platform] || {}).name || platform || ""; }
export function isUrl(text) { return /^https?:\/\//i.test(text || ""); }

// 🔴 и другие кружки в начале названия стрима путались с отметкой «в эфире» программы
export function displayTitle(title) {
  return String(title || "").replace(/^[\s\u{1F534}\u{1F7E0}-\u{1F7E2}\u26AA\u26AB]+/u, "");
}

// имя канала записи: логин из списка записей — к отображаемому имени, если оно известно
export function entryChannelName(platform, channel) {
  if (!channel) return "";
  const login = ["twitch", "kick"].includes(platform) ? String(channel).toLowerCase() : channel;
  return channelName(platform + ":" + login, channel);
}
export function isLive(channel) { return ["live", "recording", "unavailable"].includes(channel.state); }

// запись идёт или программа пытается её начать: кнопка «Остановить запись»
export function isRecording(channel) {
  return Boolean(channel) && (channel.state === "recording" || channel.state === "unavailable"
                              || (channel.temporary && channel.state === "starting"));
}

// Между «Записать» и началом записи проходит несколько секунд: streamlink
// подключается к эфиру. Всё это время кнопка показывает, что запись запускается.
// Кончается, когда запись пошла, не вышла («не удаётся записать» — та уже кнопка
// «Остановить»), эфир кончился или ждать надоело.
export const START_WAIT = 90 * 1000;
export function recordStarting(id, channel) {
  const since = state.recordStarting.get(id);
  if (!since) return false;
  const settled = (channel && !["live", "starting"].includes(channel.state)) || Date.now() - since > START_WAIT;
  if (settled) state.recordStarting.delete(id);
  return !settled;
}

export function splitId(id) {
  const index = id.indexOf(":");
  return { platform: id.slice(0, index), channel: id.slice(index + 1) };
}

export function channelName(id, fallback) {
  const profile = state.profiles.get(id);
  if (profile && profile.name) return profile.name;
  const channel = state.channels.get(id);
  return (channel && channel.label) || fallback || splitId(id).channel;
}

export function channelUrl(id) {
  const { platform, channel } = splitId(id);
  if (isUrl(channel)) return channel;
  const templates = {
    twitch: "https://www.twitch.tv/", kick: "https://kick.com/",
    youtube: "https://www.youtube.com/@", vkvideolive: "https://live.vkvideo.ru/",
  };
  return templates[platform] ? templates[platform] + channel.replace(/^@/, "") : "";
}

export function stateText(channel) {
  switch (channel.state) {
    case "recording": return t("идёт запись");
    case "live": return t("в эфире, не записывается");
    case "unavailable": return channel.message
      ? t("в эфире, но записать не удаётся: {reason}", { reason: channel.message })
      : t("в эфире, но записать не удаётся");
    case "offline": return t("не в эфире");
    case "starting": return t("проверяю…");
    case "not_found": return channel.message || t("канал не найден");
    case "error": return channel.message || t("ошибка");
    case "paused": return t("автозапись выключена");
    case "stopped": return t("наблюдение остановлено");
    default: return channel.state || "";
  }
}

export function stateTone(channel) {
  if (channel.state === "not_found" || channel.state === "error") return "error";
  if (channel.state === "unavailable") return "warn";
  return "";
}

export function sortedChannels() {
  return [...state.channels.values()].sort((a, b) =>
    (isLive(b) - isLive(a))
    || (((b.stream || {}).viewers || 0) - ((a.stream || {}).viewers || 0))
    || channelName(a.id).localeCompare(channelName(b.id), "ru"));
}

// превью идущего эфира обновляется раз в пять минут, а не при каждом событии
export function liveThumb(url) {
  url = safeUrl(url);
  if (!url) return "";
  return url + (url.includes("?") ? "&" : "?") + "sv=" + Math.floor(Date.now() / 300000);
}

export function fillAvatar(el, id, platform) {
  const profile = state.profiles.get(id);
  const url = safeUrl(profile && profile.avatar);
  if (url) {
    let img = el.querySelector("img");
    if (!img) { el.replaceChildren(); img = h("img", { alt: "" }); el.append(img); }
    setSrc(img, url);
    el.style.background = "";
    return;
  }
  const letter = (channelName(id) || "?").trim().charAt(0).toUpperCase() || "?";
  if (el.querySelector("img") || el.textContent !== letter) el.replaceChildren(letter);
  el.style.background = (PLATFORM[platform] || {}).color || "#3a3a40";
}

export const profileRequests = new Set();

// имя и аватарка, сохранённые с каналом из выдачи поиска (Kick, YouTube, VK)
export function rememberProfile(channel) {
  if (channel.profile && !state.profiles.has(channel.id)) state.profiles.set(channel.id, channel.profile);
}

// профиль для «Отслеживать», если канал знаком по выдаче поиска. Twitch
// отдаёт свежие аватарки сам — сохранённая там только устарела бы
export function knownProfile(id) {
  const profile = state.profiles.get(id);
  if (!profile || id.startsWith("twitch:")) return null;
  return { name: profile.name || "", avatar: profile.avatar || "" };
}

export async function ensureProfiles(ids) {
  // имена и аватарки всех площадок: Twitch пачкой, остальные — запросами площадок
  const wanted = ids.filter((id) => siteInfo(splitId(id).platform) && !state.profiles.has(id)
                                    && !profileRequests.has(id));
  if (!wanted.length) return;
  wanted.forEach((id) => profileRequests.add(id));
  try {
    const found = await bridge.call("profiles", wanted);
    for (const [id, profile] of Object.entries(found)) state.profiles.set(id, profile);
    if (Object.keys(found).length) {
      refreshSidebar();
      refreshView();
      if (view.onProfiles) view.onProfiles();
    }
  } catch (error) {
    // без аватарок окно обойдётся буквами
  } finally {
    wanted.forEach((id) => profileRequests.delete(id));
  }
}

export function openModal(options) {
  options = options || {};
  const root = document.getElementById("modal-root");
  const body = h("div", { class: "modal" + (options.wide ? " wide" : "") + (options.kind ? " " + options.kind : "") });
  const backdrop = h("div", { class: "modal-backdrop" }, body);
  function close() {
    backdrop.remove();
    document.removeEventListener("keydown", onKey);
    if (options.onClose) options.onClose();
  }
  // окно закрывается и крестиком, а не только Esc и щелчком мимо
  const closeButton = iconButton("close", t("Закрыть"), () => close());
  closeButton.classList.add("modal-close");
  body.append(closeButton);
  // окно поверх окна (сводка поверх проигрывателя): Esc закрывает только верхнее
  function onKey(event) { if (event.key === "Escape" && root.lastElementChild === backdrop) close(); }
  backdrop.addEventListener("mousedown", (event) => { if (event.target === backdrop) close(); });
  document.addEventListener("keydown", onKey);
  root.append(backdrop);
  return { body: body, close: close };
}

// kind — вид кнопки согласия: по умолчанию опасное действие (удаление)
export function confirmDialog(title, text, confirmLabel, onConfirm, kind) {
  const modal = openModal();
  modal.body.append(h("h2", { text: title }), h("div", { class: "muted", text: text }),
    h("div", { class: "actions" },
      button(t("Отмена"), { onclick: () => modal.close() }),
      button(confirmLabel, { kind: kind || "primary danger", onclick: () => { modal.close(); onConfirm(); } })));
}

export function qualitySelect(options, value) {
  const select = h("select");
  for (const option of options) select.append(h("option", { value: option.quality, text: t(option.label) }));
  select.value = value || state.settings.download_quality || "best";
  if (!select.value) select.value = "best";
  return select;
}

export function showStopped(text) {
  state.stopped = true;
  document.getElementById("main").replaceChildren(h("div", { class: "empty" },
    h("strong", { text: t("StreamVault не работает") }), h("span", { text: text })));
}

export function toast(text, kind, href) {
  const box = document.getElementById("toasts");
  const el = h("div", { class: "toast" + (kind ? " " + kind : "") }, h("span", { text: text }));
  if (href) {
    el.append(" ", h("a", { href: href, text: t("Открыть") }));
  }
  box.append(el);
  setTimeout(() => el.remove(), kind === "error" ? 8000 : 4000);
  while (box.children.length > 5) box.firstChild.remove();
}
