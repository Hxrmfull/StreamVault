/*
 * StreamVault, окно программы: настройки и инструменты — ffmpeg, yt-dlp,
 * TwitchDownloader и обновление самой программы.
 */

import { CHAT_MODES, DOWNLOAD_QUALITIES, bridge, button, confirmDialog, failed, fmt, h, helpTip, icon, liveQualitySelect, openModal, platformName, setText, showStopped, state, t, toast } from "./common.js";
import { render } from "./app.js";

// Полезные аргументы yt-dlp: полный список — сотни строк, здесь — то, что пригождается
export const YTDLP_TIPS = [
  ["--limit-rate 5M", "ограничить скорость, чтобы не забивать интернет"],
  ["--proxy socks5://127.0.0.1:1080", "качать через прокси"],
  ["--cookies-from-browser firefox", "войти как в браузере: записи для подписчиков, YouTube 18+"],
  ["--retries 30 --fragment-retries 30", "больше повторов, если связь рвётся"],
  ["--throttled-rate 100K", "переподключаться, если скорость упала ниже"],
  ["--force-ipv4", "если через IPv6 качается плохо"],
  ["--sleep-requests 1", "пауза между запросами, если сайт ограничивает"],
  ["--embed-thumbnail", "вшить обложку в файл"],
  ["--embed-metadata", "вшить название, автора и дату"],
  ["--embed-chapters", "вшить главы (YouTube)"],
  ["--write-subs --sub-langs ru,en --embed-subs", "субтитры (YouTube)"],
  ["--sponsorblock-remove sponsor", "вырезать рекламные вставки (YouTube)"],
  ["--js-runtimes node", "YouTube: проверки сайта через установленный Node.js"],
];

export function ytdlpArgsHelp(input) {
  const rows = YTDLP_TIPS.map(([args, note]) => h("button", {
    class: "help-row", type: "button", title: t("Добавить в поле"),
    onclick: () => {
      const current = input.value.trim();
      if (!current.split(/\s+/).includes(args.split(" ")[0])) input.value = (current ? current + " " : "") + args;
      input.focus();
    },
  }, h("code", { text: args }), h("span", { text: t(note) })));
  return [
    h("div", { class: "help-title", text: t("Полезные аргументы — щёлкни, чтобы добавить") }),
    h("div", { class: "help-rows" }, ...rows),
    h("div", { class: "help-note", text: t("Качество, имя файла и число потоков задаёт сама программа: -f, -o и -N сюда не нужны.") }),
    h("a", { href: "#", text: t("Все параметры yt-dlp →"), onclick: (event) => {
      event.preventDefault();
      bridge.call("open_url", "https://github.com/yt-dlp/yt-dlp#usage-and-options").catch(failed);
    } }),
  ];
}

export function twitchKeysHelp() {
  const steps = [
    t("Войди на dev.twitch.tv своим аккаунтом Twitch (нужна включённая двухфакторная защита)."),
    t("Your Console → Applications → Register Your Application."),
    t("Name — любое свободное имя; OAuth Redirect URLs — http://localhost; Category — Application Integration; Client Type — Confidential. Create."),
    t("Manage у нового приложения: Client ID скопируй сюда, затем New Secret — это Client Secret."),
  ];
  return [
    h("div", { class: "help-title", text: t("Ключи приложения Twitch — по желанию") }),
    h("div", { text: t("С ними программа узнаёт, что канал в эфире, через официальный API Twitch раз в 15 секунд. " +
                       "Без них — через streamlink раз в 30 секунд; запись идёт и так.") }),
    h("ol", { class: "help-steps" }, ...steps.map((step) => h("li", { text: step }))),
    h("div", { class: "help-note", text: t("Это ключи приложения, а не пароль от аккаунта. Секрет храни как пароль: " +
                                           "он лежит только в settings.json.") }),
    h("a", { href: "#", text: t("Открыть dev.twitch.tv →"), onclick: (event) => {
      event.preventDefault();
      bridge.call("open_url", "https://dev.twitch.tv/console/apps").catch(failed);
    } }),
  ];
}

export const ffmpegBlocks = new Set();
export const INSTALLING = ["checking", "downloading", "installing"];

export function ffmpegInstallText(install) {
  const progress = install.progress || {};
  switch (install.state) {
    case "checking": return t("Сверяю контрольную сумму на GitHub и gyan.dev…");
    case "downloading": {
      const params = { version: install.version || "", source: install.source || "",
                       done: fmt.size(progress.downloaded || 0), total: fmt.size(progress.total) };
      const text = install.source ? t("Скачиваю ffmpeg {version} с {source}: {done} из {total}", params)
                                  : t("Скачиваю ffmpeg {version}: {done} из {total}", params);
      return progress.speed ? text + " · " + t("{speed}/с", { speed: fmt.size(progress.speed) }) : text;
    }
    case "installing": return t("Проверяю и устанавливаю…");
    case "done": return t("ffmpeg установлен");
    case "failed": return t("Не установился: {error}", { error: install.error });
    case "cancelled": return t("Установка отменена");
    default: return "";
  }
}

/*
 * Состояние ffmpeg с кнопками. compact — плашка над страницей, пока ffmpeg
 * не найден; иначе — блок в настройках. Обновляется по событиям установки.
 */
export function ffmpegBlock(compact) {
  const box = h("div", { class: compact ? "banner ffmpeg-banner" : "ffmpeg-box" });
  box._fill = () => {
    const status = state.ffmpeg || {};
    const install = status.install || {};
    const busy = INSTALLING.includes(install.state);
    if (compact && status.path && !busy && install.state !== "done") {
      box.remove();
      return;
    }
    const body = h("div", { class: "ffmpeg-body" });
    if (status.path) {
      body.append(h("div", null, t("Найден: {path}", { path: status.path }),
        status.version ? h("span", { class: "faint", text: " · " + status.version }) : null));
    } else {
      body.append(h("div", { text: t("ffmpeg не найден: без него записи эфиров не конвертируются в mp4, " +
                                     "а часть загрузок не склеивается.") }));
    }
    const note = ffmpegInstallText(install);
    if (note) {
      body.append(h("div", { class: install.state === "failed" ? "row-sub error"
                                   : install.state === "done" ? "ok-text" : "faint", text: note }));
    }
    const progress = install.progress || {};
    if (install.state === "downloading" && progress.total) {
      const bar = h("div", { class: "bar" });
      bar.style.width = Math.min(100, (progress.downloaded || 0) * 100 / progress.total) + "%";
      body.append(h("div", { class: "progress" }, bar));
    }
    // не скачалось ни с GitHub, ни с gyan.dev — остаётся скачать руками
    const manual = install.state === "failed" && install.manual_page;
    if (manual) {
      body.append(h("div", { class: "faint", text: t("Можно скачать архив вручную в браузере: " +
        "распакуй его и укажи путь к bin\\ffmpeg.exe в настройках.") }));
    }
    const actions = h("div", { class: "toolbar" });
    if (busy) {
      actions.append(button(t("Отменить"), {
        onclick: () => bridge.call("cancel_ffmpeg_install").catch(failed) }));
    } else if (!(compact && status.path)) {
      // ffmpeg найден в системе — скачать можно только свою копию, а не «ffmpeg» вообще
      actions.append(button(status.own ? t("Обновить ffmpeg") : status.path ? t("Скачать свою копию")
                                                                          : t("Скачать ffmpeg"), {
        kind: status.path ? "" : "primary", icon: status.own ? "refresh" : "download", onclick: confirmFfmpegInstall }));
      if (manual) {
        actions.append(button(t("Скачать вручную"), {
          icon: "external", onclick: () => bridge.call("open_url", install.manual_page).catch(failed) }));
      }
      if (compact) actions.append(button(t("Указать путь"), { onclick: openSettings }));
    }
    body.append(actions);
    // плашка после удачной установки — зелёная: тревожиться больше не о чем
    box.classList.toggle("done", Boolean(compact && status.path));
    box.replaceChildren(compact ? icon(status.path ? "check" : "alert") : "", body);
  };
  box._fill();
  ffmpegBlocks.add(box);
  return box;
}

export function refreshFfmpegBlocks() {
  for (const box of ffmpegBlocks) {
    if (document.body.contains(box)) box._fill();
    else ffmpegBlocks.delete(box);
  }
}

export function confirmFfmpegInstall() {
  confirmDialog(t("Скачать ffmpeg?"),
    t("Около 115 МБ — сборки gyan.dev, на которые ссылается сайт ffmpeg.org. Качаются с GitHub, " +
      "а если он недоступен или медленный — с самого gyan.dev, с того же места. " +
      "Перед установкой программа сверит контрольную сумму в двух местах, " +
      "а ffmpeg положит в свою папку, ничего не меняя в системе."),
    t("Скачать"), () => bridge.call("install_ffmpeg").then((install) => {
      state.ffmpeg = Object.assign({}, state.ffmpeg, { install: install });
      refreshFfmpegBlocks();
    }).catch(failed));
}

export const ytdlpBlocks = new Set();

export function ytdlpUpdateText(install) {
  const progress = install.progress || {};
  switch (install.state) {
    case "checking": return t("Проверяю, есть ли новая версия…");
    case "downloading": {
      const params = { version: install.version || "", source: install.source || "",
                       done: fmt.size(progress.downloaded || 0), total: fmt.size(progress.total) };
      const text = install.source ? t("Скачиваю yt-dlp {version} с {source}: {done} из {total}", params)
                                  : t("Скачиваю yt-dlp {version}: {done} из {total}", params);
      return progress.speed ? text + " · " + t("{speed}/с", { speed: fmt.size(progress.speed) }) : text;
    }
    case "installing": return t("Проверяю и устанавливаю…");
    case "done": return t("yt-dlp обновлён до {version}", { version: install.version || "" });
    case "current": return t("Уже последняя версия");
    case "failed": return t("Не обновился: {error}", { error: install.error });
    case "cancelled": return t("Обновление отменено");
    default: return "";
  }
}

/*
 * Версия yt-dlp и кнопка обновления. compact — подсказка на странице
 * загрузок, когда что-то не скачалось; иначе — блок в настройках.
 */
export function ytdlpBlock(compact) {
  const box = h("div", { class: compact ? "banner ffmpeg-banner" : "ffmpeg-box" });
  box._fill = () => {
    const status = state.ytdlp || {};
    const install = status.install || {};
    const busy = INSTALLING.includes(install.state);
    const body = h("div", { class: "ffmpeg-body" });
    if (compact) {
      body.append(h("div", { text: t("Не скачивается? Сайты меняются, и yt-dlp перестаёт их понимать, " +
                                     "пока его не обновят.") }));
    }
    const kind = status.own ? t("обновлённая") : status.frozen ? t("встроенная") : t("установленная через pip");
    body.append(h("div", { class: compact ? "faint" : "" },
      status.version ? t("yt-dlp {version}", { version: status.version }) : t("yt-dlp не найден"),
      status.version ? h("span", { class: "faint", text: " · " + kind }) : null));
    const note = ytdlpUpdateText(install);
    if (note) {
      body.append(h("div", { class: install.state === "failed" ? "row-sub error"
                                   : ["done", "current"].includes(install.state) ? "ok-text" : "faint",
                             text: note }));
    }
    const progress = install.progress || {};
    if (install.state === "downloading" && progress.total) {
      const bar = h("div", { class: "bar" });
      bar.style.width = Math.min(100, (progress.downloaded || 0) * 100 / progress.total) + "%";
      body.append(h("div", { class: "progress" }, bar));
    }
    const actions = h("div", { class: "toolbar" });
    if (busy) {
      actions.append(button(t("Отменить"), {
        onclick: () => bridge.call("cancel_ytdlp_update").catch(failed) }));
    } else {
      actions.append(button(t("Обновить yt-dlp"), {
        kind: compact ? "primary" : "", icon: "refresh", onclick: startYtdlpUpdate }));
    }
    body.append(actions);
    box.replaceChildren(compact ? icon("alert") : "", body);
  };
  box._fill();
  ytdlpBlocks.add(box);
  return box;
}

export function refreshYtdlpBlocks() {
  for (const box of ytdlpBlocks) {
    if (document.body.contains(box)) box._fill();
    else ytdlpBlocks.delete(box);
  }
}

export function startYtdlpUpdate() {
  bridge.call("update_ytdlp").then((install) => {
    state.ytdlp = Object.assign({}, state.ytdlp, { install: install });
    refreshYtdlpBlocks();
  }).catch(failed);
}

export const twitchdownloaderBlocks = new Set();

export function twitchdownloaderInstallText(install) {
  const progress = install.progress || {};
  switch (install.state) {
    case "checking": return t("Сверяю контрольную сумму на GitHub и в каталоге scoop…");
    case "downloading": {
      const params = { version: install.version || "", source: install.source || "",
                       done: fmt.size(progress.downloaded || 0), total: fmt.size(progress.total) };
      const text = install.source ? t("Скачиваю TwitchDownloader {version} с {source}: {done} из {total}", params)
                                  : t("Скачиваю TwitchDownloader {version}: {done} из {total}", params);
      return progress.speed ? text + " · " + t("{speed}/с", { speed: fmt.size(progress.speed) }) : text;
    }
    case "installing": return t("Проверяю и устанавливаю…");
    case "done": return t("TwitchDownloader установлен");
    case "current": return t("Уже последняя версия");
    case "failed": return t("Не установился: {error}", { error: install.error });
    case "cancelled": return t("Установка отменена");
    default: return "";
  }
}

/*
 * TwitchDownloader для чата. compact — подсказка под выбором чата: видна,
 * только когда чат выбран, а TwitchDownloader нет (или он ставится).
 * chatMode — тот самый выбор.
 */
export function twitchdownloaderBlock(compact, chatMode) {
  const box = h("div", { class: compact ? "hint chat-hint" : "ffmpeg-box" });
  box._fill = () => {
    const status = state.twitchdownloader || {};
    const install = status.install || {};
    const busy = INSTALLING.includes(install.state);
    if (compact) {
      box.hidden = !(chatMode && chatMode.value !== "none" && (!status.path || busy || install.state === "failed"));
      if (box.hidden) return;
    }
    const body = h("div", { class: "ffmpeg-body" });
    if (status.path) {
      body.append(h("div", null, t("Найден: {path}", { path: status.path }),
        status.version ? h("span", { class: "faint", text: " · " + status.version }) : null));
    } else {
      body.append(h("div", { text: t("Для чата нужен TwitchDownloader (около 50 МБ): он скачивает чат Twitch " +
                                     "и рисует из него видео.") }));
    }
    const note = twitchdownloaderInstallText(install);
    if (note) {
      body.append(h("div", { class: install.state === "failed" ? "row-sub error"
                                   : ["done", "current"].includes(install.state) ? "ok-text" : "faint", text: note }));
    }
    const progress = install.progress || {};
    if (install.state === "downloading" && progress.total) {
      const bar = h("div", { class: "bar" });
      bar.style.width = Math.min(100, (progress.downloaded || 0) * 100 / progress.total) + "%";
      body.append(h("div", { class: "progress" }, bar));
    }
    const actions = h("div", { class: "toolbar" });
    if (busy) {
      actions.append(button(t("Отменить"), {
        onclick: () => bridge.call("cancel_twitchdownloader_install").catch(failed) }));
    } else {
      actions.append(button(status.own ? t("Обновить TwitchDownloader") : t("Скачать TwitchDownloader"), {
        kind: status.path ? "" : "primary", icon: status.own ? "refresh" : "download",
        onclick: () => startTwitchdownloaderInstall() }));
      if (install.state === "failed" && install.manual_page) {
        actions.append(button(t("Скачать вручную"), {
          icon: "external", onclick: () => bridge.call("open_url", install.manual_page).catch(failed) }));
      }
    }
    body.append(actions);
    box.replaceChildren(body);
  };
  if (chatMode) chatMode.addEventListener("change", () => box._fill());
  box._fill();
  twitchdownloaderBlocks.add(box);
  return box;
}

export function refreshTwitchdownloaderBlocks() {
  for (const box of twitchdownloaderBlocks) {
    if (document.body.contains(box)) box._fill();
    else twitchdownloaderBlocks.delete(box);
  }
}

export function startTwitchdownloaderInstall() {
  bridge.call("install_twitchdownloader").then((install) => {
    state.twitchdownloader = Object.assign({}, state.twitchdownloader, { install: install });
    refreshTwitchdownloaderBlocks();
  }).catch(failed);
}

export const appUpdateBlocks = new Set();
// кнопка сверху видна, когда есть что ставить
export const UPDATE_BADGE_STATES = ["available", "downloading", "installing", "done"];

export function appUpdateText(update) {
  const progress = update.progress || {};
  switch (update.state) {
    case "checking": return t("Проверяю…");
    case "current": return t("Это последняя версия");
    case "none": return t("Выпусков на GitHub пока нет");
    case "available": return t("Доступна версия {version}", { version: update.version || "" });
    case "downloading": {
      const params = { version: update.version || "", source: update.source || "",
                       done: fmt.size(progress.downloaded || 0), total: fmt.size(progress.total) };
      const text = update.source ? t("Скачиваю StreamVault {version} с {source}: {done} из {total}", params)
                                 : t("Скачиваю StreamVault {version}: {done} из {total}", params);
      return progress.speed ? text + " · " + t("{speed}/с", { speed: fmt.size(progress.speed) }) : text;
    }
    case "installing": return t("Проверяю и распаковываю…");
    case "done": return t("Версия {version} скачана и проверена", { version: update.version || "" });
    case "failed": return t("Не получилось: {error}", { error: update.error });
    case "cancelled": return t("Отменено");
    default: return "";
  }
}

/*
 * Обновление программы: версия, проверка, скачивание, перезапуск.
 * В настройках и в окошке от кнопки сверху — один и тот же блок.
 */
export function appUpdateBlock() {
  const box = h("div", { class: "ffmpeg-box" });
  box._fill = () => {
    const update = state.appUpdate || {};
    const busy = ["downloading", "installing"].includes(update.state);
    const body = h("div", { class: "ffmpeg-body" });
    body.append(h("div", { text: t("Установлена версия {version}", { version: update.current || state.info.version }) }));
    const note = appUpdateText(update);
    if (note) {
      body.append(h("div", { class: update.state === "failed" ? "row-sub error"
                                   : ["current", "done", "available"].includes(update.state) ? "ok-text" : "faint",
                             text: note }));
    }
    if (["available", "done"].includes(update.state) && update.notes) {
      body.append(h("div", { class: "update-notes", text: update.notes }));
    }
    const progress = update.progress || {};
    if (update.state === "downloading" && progress.total) {
      const bar = h("div", { class: "bar" });
      bar.style.width = Math.min(100, (progress.downloaded || 0) * 100 / progress.total) + "%";
      body.append(h("div", { class: "progress" }, bar));
    }
    const actions = h("div", { class: "toolbar" });
    const page = () => bridge.call("open_url", update.page || update.manual_page).catch(failed);
    if (busy) {
      actions.append(button(t("Отменить"), { onclick: () => bridge.call("cancel_app_update").catch(failed) }));
    } else if (update.state === "done") {
      actions.append(button(t("Перезапустить и обновить"), { kind: "primary", icon: "refresh", onclick: confirmApplyUpdate }));
    } else if (update.state === "available") {
      if (update.can_install) {
        actions.append(button(t("Скачать и установить"), { kind: "primary", icon: "download",
          onclick: () => bridge.call("install_app_update").then(setAppUpdate).catch(failed) }));
      }
      actions.append(button(update.can_install ? t("Что нового") : t("Открыть страницу выпуска"),
                            { icon: "external", onclick: page }));
    } else {
      actions.append(button(t("Проверить обновления"), { icon: "refresh", disabled: update.state === "checking",
        onclick: () => bridge.call("check_app_update").then(setAppUpdate).catch(failed) }));
      if (update.state === "failed" && update.manual_page) {
        actions.append(button(t("Открыть страницу выпуска"), { icon: "external", onclick: page }));
      }
    }
    body.append(actions);
    if (!update.can_install && update.state === "available") {
      body.append(h("div", { class: "faint", text: t("Эту копию программы отсюда не обновить: скачай выпуск " +
                                                     "со страницы GitHub (из исходников — git pull).") }));
    }
    box.replaceChildren(body);
  };
  box._fill();
  appUpdateBlocks.add(box);
  return box;
}

export function setAppUpdate(update) {
  state.appUpdate = Object.assign({}, state.appUpdate, update);
  refreshAppUpdate();
}

export function refreshAppUpdate() {
  for (const box of appUpdateBlocks) {
    if (document.body.contains(box)) box._fill();
    else appUpdateBlocks.delete(box);
  }
  const update = state.appUpdate || {};
  const badge = document.getElementById("update-badge");
  badge.hidden = !UPDATE_BADGE_STATES.includes(update.state);
  setText(badge, update.state === "done" ? t("Перезапустить и обновить")
                                         : t("Обновление {version}", { version: update.version || "" }));
}

export function openAppUpdate() {
  const modal = openModal();
  modal.body.append(h("h2", { text: t("Обновление StreamVault") }), appUpdateBlock(),
    h("div", { class: "actions" }, button(t("Закрыть"), { onclick: () => modal.close() })));
}

export function confirmApplyUpdate() {
  confirmDialog(t("Перезапустить StreamVault?"),
    // после перезапуска сами продолжаются загрузки и эфиры с автозаписью; начатое кнопкой — нет
    t("Программа закроется на несколько секунд и запустится уже новой. Загрузки продолжатся с того же места, " +
      "эфиры с автозаписью начнут писаться снова; настройки, каналы и скачанное останутся."),
    t("Перезапустить"), () => {
      state.stopped = true;
      bridge.call("apply_app_update").catch((error) => { state.stopped = false; failed(error); });
      if (bridge.browserMode) showStopped(t("StreamVault перезапускается с новой версией."));
    });
}

export function openSettings() {
  const settings = state.settings;
  const modal = openModal({ kind: "settings" });
  const fields = {};

  function text(key, label, hint, options) {
    options = options || {};
    const input = h("input", { type: options.secret ? "password" : options.number ? "number" : "text",
                               spellcheck: "false", placeholder: options.placeholder,
                               class: options.number ? "narrow" : null, step: options.step });
    if (!options.secret) input.value = settings[key] === undefined ? "" : settings[key];
    fields[key] = input;
    let control = input;
    if (options.folder && state.info.can_choose_folder) {
      control = h("div", { class: "with-button" }, input, button(t("Выбрать…"), {
        onclick: async () => {
          try {
            const chosen = await bridge.call("choose_folder", input.value);
            if (chosen) input.value = chosen;
          } catch (error) { failed(error); }
        },
      }));
    }
    if (options.secret) {
      input.placeholder = settings[key + "_set"] ? t("сохранён — впиши новый, чтобы заменить") : t("не задан");
      if (settings[key + "_set"]) {
        control = h("div", { class: "with-button" }, input, button(t("Стереть"), {
          onclick: () => { input.value = ""; input.dataset.erase = "1"; input.placeholder = t("будет стёрт при сохранении"); },
        }));
      }
    }
    // help — подсказка «?» рядом с названием; ей передаётся само поле
    return h("div", { class: "field" },
             h("label", null, label, options.help ? helpTip(() => options.help(input)) : null), control,
             hint ? h("div", { class: "hint", text: hint }) : null);
  }

  function select(key, label, options) {
    const el = h("select", null, options.map(([value, title]) => h("option", { value: value, text: title })));
    el.value = settings[key];
    fields[key] = el;
    return h("div", { class: "field" }, h("label", null, label), el);
  }

  function flag(key, label, hint) {
    const input = h("input", { type: "checkbox" });
    input.checked = Boolean(settings[key]);
    fields[key] = input;
    return h("div", { class: "field" },
      h("label", { class: "switch" }, input, h("span", { class: "track" }), label),
      hint ? h("div", { class: "hint", text: hint }) : null);
  }

  const refresh = {};
  const refreshFields = Object.keys(settings.refresh || {}).map((platform) => {
    const input = h("input", { type: "number", min: "15", class: "narrow" });
    input.value = settings.refresh[platform];
    refresh[platform] = input;
    return h("div", { class: "field" }, h("label", null, platformName(platform)), input);
  });

  const language = h("select", null, Object.entries(state.info.languages || {})
    .map(([code, name]) => h("option", { value: code, text: name })));
  language.value = state.info.language;
  fields.language = language;

  // ffmpeg: найти самой или путь вручную — с выбором файла и проверкой
  const configured = settings.ffmpeg_path && !["ffmpeg", "ffmpeg.exe"].includes(settings.ffmpeg_path.toLowerCase())
    ? settings.ffmpeg_path : "";
  const manualFfmpeg = h("input", { type: "checkbox" });
  manualFfmpeg.checked = Boolean(configured);
  const ffmpegPath = h("input", { type: "text", spellcheck: "false", placeholder: "C:\\ffmpeg\\bin\\ffmpeg.exe" });
  ffmpegPath.value = configured;
  const ffmpegNote = h("div", { class: "hint" });
  const showCheck = (ok, message) => {
    setText(ffmpegNote, message);
    ffmpegNote.className = ok ? "hint ok-text" : "hint error-text";
  };
  const ffmpegPathRow = h("div", { class: "field" }, h("div", { class: "with-button" }, ffmpegPath,
    state.info.can_choose_folder ? button(t("Выбрать файл…"), {
      onclick: async () => {
        try {
          const chosen = await bridge.call("choose_ffmpeg");
          if (chosen) {
            ffmpegPath.value = chosen.path;
            showCheck(true, t("Это ffmpeg {version}", { version: chosen.version }));
          }
        } catch (problem) { showCheck(false, problem.message); }
      },
    }) : null,
    button(t("Проверить"), {
      onclick: async () => {
        try {
          const found = await bridge.call("check_ffmpeg", ffmpegPath.value);
          showCheck(true, t("Это ffmpeg {version}", { version: found.version }));
        } catch (problem) { showCheck(false, problem.message); }
      },
    })));
  ffmpegPathRow.hidden = !manualFfmpeg.checked;
  manualFfmpeg.addEventListener("change", () => { ffmpegPathRow.hidden = !manualFfmpeg.checked; });

  // видео чата: размеры нужны, только когда видео чата делается
  const chatSize = h("div", { class: "three" },
    text("chat_width", t("Ширина видео чата"), null, { number: true }),
    text("chat_height", t("Высота"), null, { number: true }),
    text("chat_font_size", t("Шрифт"), null, { number: true }));
  const chatModeField = select("chat_mode", t("Чат при скачивании записей и клипов"),
                               CHAT_MODES.map(([mode, label]) => [mode, t(label)]));
  const liveChatField = select("live_chat", t("Чат прямых эфиров при записи"),
                               CHAT_MODES.map(([mode, label]) => [mode, t(label)]));
  const showChatSize = () => {
    chatSize.hidden = ![fields.chat_mode.value, fields.live_chat.value].includes("render");
  };
  fields.chat_mode.addEventListener("change", showChatSize);
  fields.live_chat.addEventListener("change", showChatSize);
  showChatSize();

  // деление записи на куски при конвертации: у каждого способа своё число
  const splitMode = select("split_mode", t("Делить запись на куски"),
    [["off", t("не делить")], ["size", t("по размеру")], ["duration", t("по длительности")],
     ["count", t("на равные части")]]);
  const splitFields = {
    size: text("split_size_gb", t("Кусок не больше, ГБ"),
               t("Запись делится на равные куски, каждый не больше этого: 4,1 ГБ при 2 ГБ — три куска по 1,4 ГБ, а не 2 + 2 + 0,1."),
               { number: true, step: "0.1" }),
    duration: text("split_minutes", t("Кусок длиной, минут"), null, { number: true }),
    count: text("split_count", t("На сколько частей"), null, { number: true }),
  };
  const splitHint = h("div", { class: "hint field-gap", text: t("Делится при конвертации в mp4, без перекодирования, " +
    "по ключевым кадрам — куски выходят примерно такими. Чат делится вместе с записью, в библиотеке куски — " +
    "одной карточкой и смотрятся подряд. Части эфира после обрывов делятся каждая отдельно.") });
  const showSplit = () => {
    for (const [mode, field] of Object.entries(splitFields)) field.hidden = fields.split_mode.value !== mode;
    splitHint.hidden = fields.split_mode.value === "off";
  };
  fields.split_mode.addEventListener("change", showSplit);
  showSplit();

  const subhead = (title) => h("h3", { class: "subhead", text: title });
  // разделы: слева список, справа содержимое; порядок — от частого к редкому
  const sections = [
    [t("Запись эфиров"), [
      text("root_path", t("Папка для записей"), t("Внутри появятся in-progress (пишется или ждёт конвертации) и finished (готовые записи)."), { folder: true }),
      h("div", { class: "field" }, h("label", null, t("Качество записи по умолчанию")),
        fields.quality = liveQualitySelect(settings.quality),
        h("div", { class: "hint", text: t("Если такого нет, берётся ближайшее, а у небольших каналов — исходное.") })),
      flag("convert_recordings", t("Конвертировать записи в mp4"),
           t("Включено — после эфира запись переписывается из .ts в mp4, который открывает любой " +
             "проигрыватель, телефон и телевизор. Выключено — запись остаётся в .ts и пишется на диск " +
             "один раз; .ts открывают VLC, MPC и mpv, а в mp4 её можно перевести потом кнопкой " +
             "в библиотеке.")),
      flag("mp4_faststart", t("Готовить mp4 к просмотру по сети"),
           t("Включено — видео начинает играть сразу, не скачиваясь целиком: в браузере, мессенджере, " +
             "с облака или домашнего сервера; для этого готовый mp4 переписывается на диск ещё раз. " +
             "Выключено — одной перезаписью меньше; на этом компьютере видео играет как обычно, " +
             "но по сети его сначала придётся скачать целиком.")),
      splitMode, splitFields.size, splitFields.duration, splitFields.count, splitHint,
      flag("record_from_start", t("Записывать с начала эфира, если запись началась позже"),
           t("Начало эфира скачивается из его записи на площадке и ложится первой частью, с чатом: " +
             "у Twitch и Kick — сразу, у YouTube и VK Video Live — когда эфир кончится и площадка " +
             "выложит запись. Нет записи (архив у канала выключен) — запись идёт с текущего места.")),
      h("div", { class: "two" },
        text("min_free_gb", t("Оставлять свободным на диске, ГБ"),
             t("Меньше — предупреждение, меньше пятой части — запись останавливается. 0 — не следить."), { number: true }),
        text("keep_days", t("Хранить записи, дней"),
             t("Старше — в корзину вместе с чатом, раз в час. 0 — хранить всегда. У канала может быть свой срок."),
             { number: true })),
      subhead(t("Как часто проверять каналы, секунды")),
      h("div", { class: "four" }, refreshFields),
      text("probe_timeout", t("Сколько ждать ответа streamlink, секунды"), null, { number: true })]],
    [t("Скачивание записей и клипов"), [
      text("downloads_path", t("Папка для загрузок"), t("Оставь пустым, чтобы качать в папку по умолчанию."),
           { folder: true, placeholder: settings.downloads_dir || t("downloads в папке записей") }),
      h("div", { class: "two" },
        select("download_quality", t("Качество по умолчанию"), DOWNLOAD_QUALITIES.map((o) => [o.quality, t(o.label)])),
        text("max_downloads", t("Одновременных загрузок"), null, { number: true })),
      h("div", { class: "two" },
        text("download_fragments", t("Кусков записи параллельно"), t("Больше — быстрее, если позволяет интернет"), { number: true }),
        text("ytdlp_extra_args", t("Свои аргументы yt-dlp"), t("Для опытных, например --limit-rate 5M"),
             { help: ytdlpArgsHelp }))], t("Скачивание")],
    ["Twitch", [
      subhead(t("Ключи")),
      h("div", { class: "two" },
        text("twitch_client_id", t("Client ID приложения"), t("С dev.twitch.tv: для опроса раз в 15 секунд"),
             { help: twitchKeysHelp, placeholder: t("не задан") }),
        text("twitch_client_secret", "Client Secret", null, { secret: true })),
      text("twitch_auth_token", t("Токен для записей только для подписчиков"),
           t("Кука auth-token с twitch.tv: в браузере F12 → Application → Cookies → twitch.tv. ") +
           t("Даёт полный доступ к аккаунту, как пароль — никому её не показывай."), { secret: true })]],
    [t("Чат"), [
      liveChatField,
      h("div", { class: "hint", text: t("Twitch, Kick, YouTube и VK Video Live: чат читается анонимно, " +
        "как у зрителя без входа, пока идёт запись, и ложится рядом с ней.") }),
      chatModeField,
      h("div", { class: "hint field-gap", text: t("Файлы чата: .chat.html — читать в браузере, " +
        ".chat.json — полный чат, .chat.mp4 — видео чата. Их делает TwitchDownloader (раздел «Инструменты»); " +
        "смайлы 7TV, BTTV и FFZ он подставляет в чат Twitch.") }),
      chatSize]],
    state.info.tray ? [t("Работа в фоне"), [
      flag("close_to_tray", t("Сворачивать в трей при закрытии окна"),
           t("Записи и загрузки продолжаются. Выйти — через значок в трее или кнопку выхода сверху.")),
      flag("tray_notifications", t("Уведомлять о начале эфира, записи и о скачанном"),
           t("Пока окно свёрнуто в трей.")),
      flag("autostart", t("Запускать вместе с Windows"),
           t("Сразу в трей, без окна — чтобы записи шли и после перезагрузки."))]] : null,
    [t("Инструменты"), [
      h("div", { class: "hint field-gap", text: t("Кнопки этого раздела срабатывают сразу, без «Сохранить».") }),
      subhead("ffmpeg"),
      ffmpegBlock(false),
      h("div", { class: "field" },
        h("label", { class: "switch" }, manualFfmpeg, h("span", { class: "track" }), t("Указать путь к ffmpeg вручную")),
        h("div", { class: "hint", text: t("Иначе программа ищет его сама: своя копия, PATH, winget, scoop, chocolatey, C:\\ffmpeg.") })),
      ffmpegPathRow, ffmpegNote,
      subhead("TwitchDownloader"),
      twitchdownloaderBlock(false),
      subhead("yt-dlp"),
      ytdlpBlock(false),
      h("div", { class: "hint", text: t("Сайты меняются, и старый yt-dlp перестаёт их понимать. " +
        "Обновление проверяется подписью разработчиков yt-dlp.") })]],
    [t("Программа"), [
      h("div", { class: "field" }, h("label", null, "Язык · Language"), language),
      subhead(t("Обновления")),
      appUpdateBlock(),
      flag("check_updates", t("Проверять обновления при запуске"),
           t("Программа спрашивает GitHub, нет ли версии новее, — больше никуда и ничего о вас не отправляет. " +
             "Скачанное ставится, только если совпадает с контрольной суммой, которую сообщает GitHub.")),
      select("log_level", t("Подробность журнала"),
             [["debug", t("всё, для отладки")], ["info", t("обычная")], ["warning", t("только предупреждения")], ["error", t("только ошибки")]])]],
  ].filter(Boolean);

  const nav = h("nav", { class: "settings-nav" });
  const content = h("div", { class: "settings-body" });
  // short — имя раздела в списке слева, если полное не помещается
  const blocks = sections.map(([title, children, short]) => {
    const block = h("fieldset", null, h("legend", null, title), children);
    const link = h("button", { type: "button", text: short || title });
    link.addEventListener("click", () => content.scrollTo({ top: block.offsetTop - content.offsetTop - 14,
                                                             behavior: "smooth" }));
    nav.append(link);
    content.append(block);
    return { block, link };
  });
  // раздел, до которого прокрутили, подсвечен слева
  const markSection = () => {
    const top = content.scrollTop + 40;
    let current = blocks[0];
    for (const item of blocks) if (item.block.offsetTop - content.offsetTop <= top) current = item;
    if (content.scrollTop + content.clientHeight >= content.scrollHeight - 2) current = blocks[blocks.length - 1];
    blocks.forEach((item) => item.link.classList.toggle("active", item === current));
  };
  content.addEventListener("scroll", markSection);

  const error = h("div", { class: "error-text" });
  const save = button(t("Сохранить"), { kind: "primary", type: "submit" });
  const form = h("form", { class: "settings-form" },
    h("div", { class: "settings-head" }, h("h2", { text: t("Настройки") })),
    h("div", { class: "settings-main" }, nav, content),
    h("div", { class: "settings-foot" }, error,
      button(t("Отмена"), { onclick: () => modal.close() }), save));

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setText(error, "");
    const changes = {};
    for (const [key, input] of Object.entries(fields)) {
      if (input.type === "checkbox") {
        if (input.checked !== Boolean(settings[key])) changes[key] = input.checked;
        continue;
      }
      if (input.type === "password") {
        if (input.value) changes[key] = input.value;
        else if (input.dataset.erase) changes[key] = "";
        continue;
      }
      const value = input.value;
      if (String(settings[key]) !== value) changes[key] = input.type === "number" ? Number(value) : value;
    }
    const intervals = {};
    let intervalsChanged = false;
    for (const [platform, input] of Object.entries(refresh)) {
      intervals[platform] = Number(input.value);
      if (intervals[platform] !== settings.refresh[platform]) intervalsChanged = true;
    }
    if (intervalsChanged) changes.refresh = intervals;
    const ffmpegValue = manualFfmpeg.checked ? ffmpegPath.value.trim() : "";
    if (ffmpegValue !== configured) changes.ffmpeg_path = ffmpegValue;
    if (!Object.keys(changes).length) { modal.close(); return; }
    save.disabled = true;
    try {
      const result = await bridge.call("save_settings", changes);
      state.settings = result.settings;
      modal.close();
      // новый язык — проще всего перерисовать окно целиком
      if (changes.language) { location.reload(); return; }
      if ("ffmpeg_path" in changes) {
        state.ffmpeg = await bridge.call("ffmpeg_status");
        render();
      }
      toast(result.restart ? t("Сохранено. Часть изменений заработает после перезапуска программы")
                           : t("Настройки сохранены"), "ok");
    } catch (problem) {
      setText(error, problem.message);
    } finally {
      save.disabled = false;
    }
  });
  modal.body.append(form);
  markSection();
}
