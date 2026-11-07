/*
 * StreamVault, окно программы: «Загрузки» — очередь, прогресс, пауза и отмена.
 */

import { CHAT_STAGES, DOWNLOAD_QUALITIES, bridge, button, channelName, chatSupported, failed, fmt, h, icon, iconButton, liveThumb, platformName, setSrc, setText, state, syncList, t } from "./common.js";
import { view } from "./app.js";
import { requestPreview } from "./library.js";
import { openLive } from "./player.js";

export function renderDownloads(main) {
  view.jobs = h("div", { class: "jobs" });
  // клипы и видео с чатом поверх: их ход — здесь же, над загрузками
  view.edits = h("div", { class: "jobs" });
  view.editsBox = h("div", { class: "edits" }, h("h3", { class: "subhead", text: t("Правка записей") }), view.edits);
  // идущие записи эфиров: сколько записано, скорость, часть, чат
  view.records = h("div", { class: "jobs" });
  view.recordsBox = h("div", { class: "edits" }, h("h3", { class: "subhead", text: t("Идёт запись") }), view.records);
  view.empty = h("div", { class: "empty" },
    h("strong", { text: t("Загрузок пока нет") }),
    t("Вставь ссылку на запись или клип в строку сверху либо найди там канал и выбери его записи."));
  main.append(
    h("div", { class: "toolbar" },
      h("h1", { class: "page-title", text: t("Загрузки") }), h("span", { class: "spacer" }),
      button(t("Открыть папку"), { icon: "folder", onclick: () => bridge.call("open_folder", "downloads").catch(failed) }),
      // убирает только строки списка — файлы остаются; корзина тут пугала бы
      view.clearFinished = button(t("Очистить список"), {
        icon: "close", title: t("Убрать завершённые загрузки из списка; файлы останутся на диске"),
        onclick: () => bridge.call("clear_finished").catch(failed) })),
    view.recordsBox, view.editsBox, view.empty, view.jobs);
  view.refresh = refreshDownloads;
  refreshDownloads();
}

export function refreshDownloads() {
  const jobs = [...state.jobs.values()].sort((a, b) => b.created - a.created);
  const edits = [...state.edits.values()].sort((a, b) => b.created - a.created);
  view.editsBox.hidden = edits.length === 0;
  syncList(view.edits, edits, (job) => job.id, createJobRow, updateEditRow);
  const records = [...state.channels.values()].filter((channel) => channel.recording)
    .sort((a, b) => a.recording.started - b.recording.started);
  view.recordsBox.hidden = records.length === 0;
  syncList(view.records, records, (channel) => channel.id, createJobRow, updateRecordRow);
  view.empty.hidden = jobs.length > 0 || edits.length > 0 || records.length > 0;
  view.clearFinished.hidden = !jobs.some((job) => ["done", "cancelled", "failed"].includes(job.state));
  syncList(view.jobs, jobs, (job) => job.id, createJobRow, updateJobRow);
}

export function refreshDownloadsCount() {
  const active = [...state.jobs.values()].filter((job) =>
    ["queued", "downloading", "processing"].includes(job.state)).length
    + [...state.edits.values()].filter((job) => ["queued", "running"].includes(job.state)).length;
  const el = document.getElementById("downloads-count");
  el.hidden = active === 0;
  setText(el, active);
}

export function createJobRow() {
  const row = h("div", { class: "job" });
  row._img = h("img", { alt: "", loading: "lazy" });
  row._img.hidden = true;
  row._title = h("div", { class: "job-title" });
  row._sub = h("div", { class: "job-sub" });
  row._bar = h("div", { class: "bar" });
  row._progress = h("div", { class: "progress" }, row._bar);
  row._status = h("div", { class: "job-status" });
  row._actions = h("div", { class: "job-actions" });
  row.append(h("div", { class: "thumb" }, h("div", { class: "placeholder" }, icon("video")), row._img),
             h("div", { class: "job-main" }, row._title, row._sub, row._progress, row._status),
             row._actions);
  return row;
}

// строка идущей записи: эфир, сколько записано, скорость, часть, чат
export function updateRecordRow(row, channel) {
  const recording = channel.recording;
  const stream = channel.stream || {};
  setText(row._title, stream.title || channelName(channel.id));
  row._title.title = recording.file || "";
  setText(row._sub, [channelName(channel.id), platformName(channel.platform), qualityText(recording.quality),
                     recording.part > 1 ? t("часть {part}", { part: recording.part }) : ""].filter(Boolean).join(" · "));
  // кадр эфира — как на карточке в «Эфирах»: свежий раз в пять минут
  const thumb = liveThumb(stream.thumbnail);
  if (thumb && row._img.dataset.src !== thumb) {
    row._img.dataset.src = thumb;
    setSrc(row._img, thumb);
  }
  // сколько всего будет — неизвестно, поэтому не полоса (она выглядела как ожидание), а
  // «REC» с мигающей точкой на кадре и показатели, которые растут на глазах
  if (!row._rec) {
    row._rec = h("span", { class: "badge rec rec-corner", text: "REC" });
    row._img.parentNode.append(row._rec);
    row._stats = {};
    const cell = (key, label) => {
      row._stats[key] = h("strong");
      return h("div", { class: "rec-stat" }, row._stats[key], h("span", { text: label }));
    };
    row._status.replaceWith(h("div", { class: "rec-stats" }, cell("time", t("записано")), cell("size", t("размер")),
                                     cell("speed", t("скорость")), cell("chat", t("сообщений чата"))));
  }
  row._progress.hidden = true;
  const speed = (state.recordSpeeds.get(channel.id) || {}).speed;
  setText(row._stats.time, fmt.duration(Date.now() / 1000 - recording.started));
  setText(row._stats.size, fmt.size(recording.bytes || 0));
  setText(row._stats.speed, speed ? t("{speed}/с", { speed: fmt.size(speed) }) : "…");
  const messages = recording.chat_messages;
  row._stats.chat.parentNode.hidden = messages === null || messages === undefined;
  // точное число, а не «1,5 тыс.»: счётчик растёт на глазах
  setText(row._stats.chat, Number(messages || 0).toLocaleString());
  if (row._actions.dataset.key === channel.id) return;
  row._actions.dataset.key = channel.id;
  row._actions.replaceChildren(
    iconButton("play", t("Смотреть"), () => openLive(channel.id)),
    iconButton("folder", t("Показать в папке"), () => bridge.call("show_in_folder", channel.recording.file).catch(failed)),
    iconButton("stop", t("Остановить запись"), () => bridge.call("stop_recording", channel.id).catch(failed)));
}

// сколько примерно осталось: по прошедшему времени и сделанной доле
function editTimeLeft(job) {
  const percent = job.progress || 0;
  const passed = Date.now() / 1000 - (job.started || 0);
  if (!job.started || percent < 2 || percent >= 100 || passed < 5) return "";
  return t("осталось {time}", { time: fmt.duration(Math.max(1, passed * (100 - percent) / percent)) });
}

// строка правки: клип, видео с чатом поверх или деление на куски
export function updateEditRow(row, job) {
  // кадр — из записи, а когда готово — из результата (у поделённой — первый кусок):
  // исходника к тому времени на месте может уже не быть
  const framed = job.state === "done" ? job.target : job.source;
  if (framed && row._framed !== framed) {
    row._framed = framed;
    requestPreview(framed, (preview) => {
      if (row._framed === framed && preview && preview.thumbnail) setSrc(row._img, preview.thumbnail);
    });
  }
  setText(row._title, job.name);
  row._title.title = job.target;
  setText(row._sub, job.kind === "clip"
    ? [t("клип {start}–{end}", { start: fmt.duration(job.start), end: fmt.duration(job.end) }),
       job.with_chat ? t("с чатом") : ""].filter(Boolean).join(" · ")
    : job.kind === "split" ? t("деление на части")
    : t("видео чата поверх записи"));
  row._progress.className = "progress" + (job.state === "running" && job.kind === "clip" ? " indeterminate" : "")
    + (job.state === "failed" ? " failed" : "");
  row._bar.style.width = (job.progress || 0) + "%";
  row._progress.hidden = !["queued", "running"].includes(job.state);
  setText(row._status, {
    queued: t("в очереди"),
    running: job.kind === "clip" ? t("вырезаю…")
      : [job.kind === "split" ? (job.progress || 0) + "%"
         : t("накладываю чат · {percent}%", { percent: job.progress || 0 }), editTimeLeft(job)].filter(Boolean).join(" · "),
    done: job.kind === "split" && (job.pieces || []).length
      ? t("Готово · частей: {count}", { count: job.pieces.length })
      : job.chat_messages ? t("Готово · чат: {count}", { count: job.chat_messages }) : t("Готово"),
    failed: t("ошибка: {error}", { error: job.error || t("неизвестная") }),
    cancelled: t("отменено"),
  }[job.state] || job.state);
  row._status.className = "job-status" + (job.state === "failed" ? " error" : job.state === "done" ? " ok-text" : "");
  if (row._actions.dataset.key === job.state) return;
  row._actions.dataset.key = job.state;
  const call = (name, ...args) => () => bridge.call(name, ...args).catch(failed);
  const actions = [];
  if (["queued", "running"].includes(job.state)) {
    actions.push(iconButton("close", t("Отменить"), call("cancel_edit", job.id)));
  }
  if (job.state === "done") {
    actions.push(iconButton("play", t("Открыть"), call("open_file", job.target)),
                 iconButton("folder", t("Показать в папке"), call("show_in_folder", job.target)));
  }
  row._actions.replaceChildren(...actions);
}

export function sectionText(section) {
  return section ? t("отрезок {start}–{end}", { start: fmt.duration(section[0]),
                                                    end: fmt.duration(section[1]) }) : "";
}

export function qualityText(quality) {
  // "h264:1080" — начало эфира: те же кодеки, что у записи; качество то же
  const plain = String(quality || "").replace(/^h264:/, "");
  const known = DOWNLOAD_QUALITIES.find((option) => option.quality === plain);
  return known ? t(known.label) : quality;
}

export function progressText(job) {
  const progress = job.progress || {};
  const parts = [];
  if (progress.percent !== null && progress.percent !== undefined) parts.push(Math.floor(progress.percent) + "%");
  if (progress.downloaded) {
    parts.push(progress.total
      ? t("{done} из {total}", { done: fmt.size(progress.downloaded), total: fmt.size(progress.total) })
      : t("скачано {size}", { size: fmt.size(progress.downloaded) }));
  }
  if (progress.speed) parts.push(t("{speed}/с", { speed: fmt.size(progress.speed) }));
  if (progress.eta !== null && progress.eta !== undefined && progress.percent < 100) parts.push(t("осталось {time}", { time: fmt.duration(progress.eta) }));
  return parts.join(" · ") || t("начинаю…");
}

// начало эфира и его склейка с записью (live/joining.py): идёт, готово или почему нет
export function joinText(job) {
  if (job.joined) return t("склеено с записью");
  const join = job.file && state.joins.get(job.file);
  if (!join) return "";
  if (join.state === "joining") return t("склеивается с записью: {percent}%", { percent: join.percent || 0 });
  if (join.state === "done") return t("склеено с записью");
  return t("не склеено с записью: {reason}", { reason: join.reason || "?" });
}

// размер скачанного — из последнего прогресса загрузки
export function doneSize(job) {
  const progress = job.progress || {};
  const size = job.size || progress.total || progress.downloaded;
  return size ? fmt.size(size) : "";
}

export function updateJobRow(row, job) {
  setSrc(row._img, job.thumbnail);
  setText(row._title, job.title || job.url);
  row._title.title = job.title || job.url;
  setText(row._sub, [job.channel, platformName(job.platform), qualityText(job.quality),
                     sectionText(job.section)].filter(Boolean).join(" · "));

  const chatProgress = job.state === "processing" && job.chat_progress;
  const percent = chatProgress ? chatProgress.percent : job.progress && job.progress.percent;
  const active = job.state === "downloading" || job.state === "processing";
  row._progress.className = "progress"
    + (active && (percent === null || percent === undefined || (job.state === "processing" && !chatProgress)) ? " indeterminate" : "")
    + (job.state === "done" ? " done" : "") + (job.state === "failed" ? " failed" : "");
  row._bar.style.width = job.state === "done" ? "100%"
    : (percent !== null && percent !== undefined && (job.state !== "processing" || chatProgress) ? percent + "%" : "");
  // у готовой загрузки полоса ничего не сообщает
  row._progress.hidden = job.state === "cancelled" || job.state === "done";

  const statusText = {
    // начало эфира не скачалось — программа повторит сама
    queued: job.retry_at && job.retry_at * 1000 > Date.now()
      ? t("повтор в {time}: {error}", { time: new Date(job.retry_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
                                       error: job.reason || job.error || "?" })
      : t("в очереди"),
    downloading: progressText(job),
    processing: chatProgress
      ? t(CHAT_STAGES[chatProgress.stage] || CHAT_STAGES.download) + (chatProgress.percent !== null && chatProgress.percent !== undefined
                                                       ? " · " + chatProgress.percent + "%" : "…")
      : job.chat_only ? t("скачиваю чат…") : t("склеиваю и перепаковываю…"),
    paused: t("на паузе") + (percent ? " · " + Math.floor(percent) + "%" : "")
            + " — " + t("продолжится с того же места"),
    done: [t("Готово"), doneSize(job), joinText(job),
           job.chat_error ? t("чат: {error}", { error: job.chat_error }) : ""].filter(Boolean).join(" · "),
    // начало эфира: почему не взять — коротко; текст ошибки — в журнале
    failed: job.reason ? t("начало эфира не скачать: {reason}", { reason: job.reason })
                       : t("ошибка: {error}", { error: job.error || t("неизвестная") }),
    cancelled: t("отменено"),
  }[job.state] || job.state;
  setText(row._status, statusText);
  row._status.title = job.reason && job.error ? job.error : "";
  row._status.title = job.state === "done" && job.file ? job.file : "";
  row._status.className = "job-status" + (job.state === "failed" ? " error" : job.state === "done" ? " ok-text" : "");

  const chatFiles = job.chat_files || {};
  const actionsKey = [job.state, job.chat_only, chatFiles.html, chatFiles.video].join("|");
  if (row._actions.dataset.key === actionsKey) return;
  row._actions.dataset.key = actionsKey;
  const call = (name, ...args) => () => bridge.call(name, ...args).catch(failed);
  const actions = [];
  if (job.state === "queued" || job.state === "downloading") {
    actions.push(iconButton("pause", t("Пауза"), call("pause_download", job.id)));
  }
  if (["queued", "downloading", "processing", "paused", "failed"].includes(job.state)) {
    actions.push(iconButton("close", t("Отменить и удалить недокачанное"), call("cancel_download", job.id)));
  }
  if (job.state === "paused") actions.push(iconButton("play", t("Продолжить"), call("resume_download", job.id)));
  if (job.state === "failed") actions.push(iconButton("refresh", t("Повторить"), call("resume_download", job.id)));
  if (job.state === "cancelled") actions.push(iconButton("download", t("Скачать заново"), call("resume_download", job.id)));
  if (job.state === "done" && job.file) {
    actions.push(iconButton("play", t("Открыть"), call("open_file", job.file)),
                 iconButton("folder", t("Показать в папке"), call("show_in_folder", job.file)));
    if (chatFiles.html) actions.push(iconButton("chat", t("Открыть чат"), call("open_file", chatFiles.html)));
    if (chatFiles.video) actions.push(iconButton("video", t("Открыть видео чата"), call("open_file", chatFiles.video)));
    // скачать чат — словами: значок чата рядом означает «открыть»
    if (!chatFiles.html && chatSupported(job)) {
      actions.push(button(t("Скачать чат"), { kind: "ghost", icon: "download", onclick: call("download_chat", job.id) }));
    }
  }
  if (["done", "cancelled", "failed", "paused"].includes(job.state)) {
    actions.push(iconButton("close", t("Убрать из списка; файл останется"), call("remove_download", job.id)));
  }
  row._actions.replaceChildren(...actions);
}
