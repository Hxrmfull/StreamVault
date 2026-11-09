/*
 * StreamVault, окно программы: «Загрузки» — очередь, прогресс, пауза и отмена.
 */

import { CHAT_ONLY, CHAT_STAGES, DOWNLOAD_QUALITIES, bridge, fillChannelLine, jobChannelId, button, channelName, chatSupported, failed, fmt, h, icon, iconButton, liveThumb, platformName, setSrc, setText, state, syncList, t, recordingTroubleText} from "./common.js";
import { view } from "./app.js";
import { requestPreview } from "./library.js";
import { openLive } from "./player.js";

// вкладки «Загрузок»: идущие записи, склейка начала эфира с записью (кончилась — строка
// уходит в «Загрузки»), что докачивается, чаты без видео, правка
const DOWNLOAD_TABS = [["records", "Записи"], ["joining", "Склеивается"], ["downloads", "Загрузки"],
                       ["chats", "Чаты"], ["edits", "Редактирование"]];

// чат без видео: «только чат», из стороннего архива, пропуски чата — своей вкладкой
// (archive/downloads.py: is_chat_job)
const CHAT_JOBS = new Set(["chat", "chat-archive", "chat-gaps"]);
// чат к своей записи: файл загрузки — сама запись
const TO_RECORDING = new Set(["chat-archive", "chat-gaps"]);
function isChatJob(job) {
  return CHAT_JOBS.has(job.quality);
}

// загрузка начала эфира, которое сейчас склеивается с записью
function isJoining(job) {
  const join = job.state === "done" && !job.joined && job.file && state.joins.get(job.file);
  return Boolean(join) && join.state === "joining";
}

// во вкладке «Склеивается» — и то, что склейки ещё ждёт: видно, что она будет
function inJoinTab(job) {
  return isJoining(job) || awaitingJoin(job);
}

// запись канала этого начала эфира ещё идёт — склейка после её конца
export function recordingGoes(job) {
  const channel = state.channels.get(jobChannelId(job));
  return Boolean(channel && channel.recording);
}

function chosenTab(counts) {
  let tab = "";
  try { tab = sessionStorage.getItem("downloads-tab") || ""; } catch (error) { /* хранилище закрыто */ }
  if (DOWNLOAD_TABS.some(([name]) => name === tab)) return tab;
  return counts.records ? "records" : counts.joining ? "joining" : "downloads";
}

export function renderDownloads(main) {
  view.jobs = h("div", { class: "jobs" });
  view.tabs = h("div", { class: "tabs" });
  view.tabButtons = {};
  for (const [name, label] of DOWNLOAD_TABS) {
    const count = h("span", { class: "tab-count" });
    const tab = h("button", { type: "button" }, t(label), count);
    tab._count = count;
    tab.addEventListener("click", () => {
      view.downloadsTab = name;
      try { sessionStorage.setItem("downloads-tab", name); } catch (error) { /* не помним — не беда */ }
      refreshDownloads();
    });
    view.tabButtons[name] = tab;
    view.tabs.append(tab);
  }
  view.downloadsTab = null;
  // клипы и видео с чатом поверх: их ход — здесь же, над загрузками
  view.edits = h("div", { class: "jobs" });
  view.editsBox = h("div", { class: "edits" }, view.edits);
  view.editsEmpty = h("div", { class: "empty" }, h("strong", { text: t("Список пуст") }),
    t("Клипы, деление на части и конвертация из библиотеки появятся здесь."));
  // идущие записи эфиров: сколько записано, скорость, часть, чат
  view.records = h("div", { class: "jobs" });
  view.recordsBox = h("div", { class: "edits" }, view.records);
  view.recordsEmpty = h("div", { class: "empty" }, h("strong", { text: t("Сейчас ничего не пишется") }),
    t("Начни запись на странице канала или включи автозапись — идущие записи появятся здесь."));
  // склейка начала эфира с записью: её ход — своей вкладкой, готовое уходит в «Загрузки»
  view.joining = h("div", { class: "jobs" });
  view.joiningEmpty = h("div", { class: "empty" }, h("strong", { text: t("Сейчас ничего не склеивается") }),
    t("Когда запись началась посреди эфира, его начало докачивается и после записи склеивается с ней — ход виден здесь."));
  // чаты без видео: скачанные «только чат», из архива, докачанные пропуски
  view.chats = h("div", { class: "jobs" });
  view.chatsEmpty = h("div", { class: "empty" }, h("strong", { text: t("Чатов пока нет") }),
    t("Чаты, скачанные без видео, из стороннего архива и докачанные пропуски чата появятся здесь."));
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
        onclick: () => (view.downloadsTab === "edits" ? bridge.call("clear_edits")
          : bridge.call("clear_finished", view.downloadsTab)).catch(failed) })),
    view.tabs, view.recordsBox, view.recordsEmpty, view.joining, view.joiningEmpty, view.editsBox, view.editsEmpty,
    view.empty, view.jobs, view.chatsEmpty, view.chats);
  view.refresh = refreshDownloads;
  refreshDownloads();
}

export function refreshDownloads() {
  const all = [...state.jobs.values()].sort((a, b) => b.created - a.created);
  const joining = all.filter(inJoinTab);
  const chats = all.filter((job) => !inJoinTab(job) && isChatJob(job));
  const jobs = all.filter((job) => !inJoinTab(job) && !isChatJob(job));
  const edits = [...state.edits.values()].sort((a, b) => b.created - a.created);
  const records = [...state.channels.values()].filter((channel) => channel.recording)
    .sort((a, b) => a.recording.started - b.recording.started);
  // у вкладки — сколько в ней строк
  const counts = { records: records.length, joining: joining.length, downloads: jobs.length, chats: chats.length,
                   edits: edits.length };
  const tab = view.downloadsTab || chosenTab(counts);
  view.downloadsTab = tab;
  // во вкладке что-то идёт — её название переливается фиолетовым: видно, не открывая
  const busy = {
    records: records.length > 0,
    joining: joining.length > 0,
    downloads: jobs.some((job) => ["queued", "downloading", "processing"].includes(job.state)),
    chats: chats.some((job) => ["queued", "downloading", "processing"].includes(job.state)),
    edits: edits.some((job) => ["queued", "running"].includes(job.state)),
  };
  for (const [name, button] of Object.entries(view.tabButtons)) {
    button.classList.toggle("active", name === tab);
    button.classList.toggle("busy", busy[name]);
    setText(button._count, counts[name] ? String(counts[name]) : "");
  }
  view.recordsBox.hidden = tab !== "records";
  view.recordsEmpty.hidden = tab !== "records" || records.length > 0;
  view.editsBox.hidden = tab !== "edits";
  view.editsEmpty.hidden = tab !== "edits" || edits.length > 0;
  view.joining.hidden = tab !== "joining";
  view.joiningEmpty.hidden = tab !== "joining" || joining.length > 0;
  view.jobs.hidden = tab !== "downloads";
  view.empty.hidden = tab !== "downloads" || jobs.length > 0;
  view.chats.hidden = tab !== "chats";
  view.chatsEmpty.hidden = tab !== "chats" || chats.length > 0;
  syncList(view.edits, edits, (job) => job.id, createJobRow, updateEditRow);
  syncList(view.records, records, (channel) => channel.id, createJobRow, updateRecordRow);
  const finished = (list) => list.some((job) => ["done", "cancelled", "failed"].includes(job.state));
  view.clearFinished.hidden = tab === "records" || tab === "joining"
    || !finished(tab === "edits" ? edits : tab === "chats" ? chats : jobs);
  view.clearFinished.title = tab === "edits" ? t("Убрать законченное из списка; файлы останутся на диске")
    : t("Убрать завершённые загрузки из списка; файлы останутся на диске");
  syncList(view.jobs, jobs, (job) => job.id, createJobRow, updateJobRow);
  syncList(view.chats, chats, (job) => job.id, createJobRow, updateJobRow);
  syncList(view.joining, joining, (job) => job.id, createJobRow, updateJobRow);
}

export function refreshDownloadsCount() {
  const active = [...state.jobs.values()].filter((job) =>
    ["queued", "downloading", "processing"].includes(job.state)).length
    + [...state.edits.values()].filter((job) => ["queued", "running"].includes(job.state)).length;
  const el = document.getElementById("downloads-count");
  el.hidden = active === 0;
  setText(el, active);
  // что-то идёт в любой вкладке «Загрузок» — запись, склейка, загрузка, правка: «Загрузки»
  // в верхнем меню переливаются так же, как название вкладки
  const busy = active > 0 || [...state.channels.values()].some((channel) => channel.recording)
    || [...state.joins.values()].some((join) => join.state === "joining")
    || [...state.jobs.values()].some(inJoinTab);          // как у вкладки «Склеивается»
  const link = el.closest("a");
  if (link) link.classList.toggle("busy", busy);
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
  fillChannelLine(row._sub, channel.id, channelName(channel.id),
                  [platformName(channel.platform), qualityText(recording.quality),
                   recording.part > 1 ? t("часть {part}", { part: recording.part }) : ""]);
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
  // начало этого эфира (part00): качается, ждёт повтора, склеено — прямо в строке записи
  const beginning = beginningOf(recording);
  if (!row._beginning) {
    row._beginning = h("div", { class: "job-status rec-beginning" });
    row.querySelector(".job-main").append(row._beginning);
  }
  row._beginning.hidden = !beginning;
  if (beginning) setText(row._beginning, t("Начало эфира: {status}", { status: beginningStatus(beginning) }));
  // запись не успевает или площадка, похоже, режет скачивание — строкой предупреждения
  if (!row._trouble) {
    row._trouble = h("div", { class: "job-status warn-text rec-trouble" });
    row.querySelector(".job-main").append(row._trouble);
  }
  const trouble = recordingTroubleText(channel);
  row._trouble.hidden = !trouble;
  setText(row._trouble, trouble);
  if (row._actions.dataset.key === channel.id) return;
  row._actions.dataset.key = channel.id;
  row._actions.classList.add("rec-actions");
  row._actions.replaceChildren(
    button(t("Смотреть"), { icon: "play", onclick: () => openLive(channel.id) }),
    iconButton("folder", t("Показать в папке"), () => bridge.call("show_in_folder", channel.recording.file).catch(failed)),
    button(t("Остановить"), { icon: "stop", kind: "stop", title: t("Остановить запись — записанное уйдёт в обработку"),
                              onclick: () => bridge.call("stop_recording", channel.id).catch(failed) }));
}

// загрузка начала эфира этой записи: «… - part00» с тем же именем, что у записи
function beginningOf(recording) {
  const base = (path) => String(path || "").split(/[\\/]/).pop().replace(/\.[^.]+$/, "");
  const name = base(recording.file);
  return [...state.jobs.values()].find((job) => job.fixed_target && base(job.fixed_target + ".x") === name + " - part00");
}

function beginningStatus(job) {
  if (job.state === "queued" && job.retry_at && job.retry_at * 1000 > Date.now()) {
    return t("повтор в {time}: {error}", { time: new Date(job.retry_at * 1000).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }),
                                         error: job.reason || job.error || "?" });
  }
  if (job.state === "queued") return t("в очереди");
  if (job.state === "downloading") return progressText(job);
  if (job.state === "processing") return t("склейка и перепаковка…");
  if (job.state === "done") return joinText(job) || t("скачано");
  if (job.state === "failed") return job.reason || job.error || t("не скачалось");
  return job.state;
}

// сколько примерно осталось: по прошедшему времени и сделанной доле
function editTimeLeft(job) {
  const percent = job.progress || 0;
  const passed = Date.now() / 1000 - (job.started || 0);
  if (!job.started || percent < 2 || percent >= 100 || passed < 5) return "";
  return t("осталось {time}", { time: fmt.duration(Math.max(1, passed * (100 - percent) / percent)) });
}

// названия форматов конвертации — короткие, для строки «конвертация в …»
const CONVERT_NAMES = { mp4: "mp4", mkv: "mkv", audio: "m4a" };

export function convertingText(started) {
  return t("конвертация… · {time}", { time: fmt.duration(Math.max(0, Date.now() / 1000 - (started || Date.now() / 1000))) });
}

// строка правки: клип, видео с чатом поверх, деление на части или конвертация
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
    : job.kind === "convert" ? t("конвертация в {format}", { format: CONVERT_NAMES[job.format] || job.format })
    : t("видео чата поверх записи"));
  row._progress.className = "progress" + (job.state === "running" && job.kind === "clip" ? " indeterminate" : "")
    + (job.state === "failed" ? " failed" : "");
  row._bar.style.width = (job.progress || 0) + "%";
  // у конвертации доли нет (ffmpeg перекладывает без перекодирования, за секунды-минуты):
  // не полоса, а время, которое идёт, — видно, что работа не встала
  row._progress.hidden = !["queued", "running"].includes(job.state) || job.kind === "convert";
  if (job.kind === "convert" && job.state === "running" && job.started) row._status.dataset.convertSince = job.started;
  else delete row._status.dataset.convertSince;
  setText(row._status, {
    queued: t("в очереди"),
    running: job.kind === "convert" ? convertingText(job.started)
      : job.kind === "clip" ? t("вырезание…")
      : [job.kind === "split" ? (job.progress || 0) + "%"
         : t("наложение чата · {percent}%", { percent: job.progress || 0 }), editTimeLeft(job)].filter(Boolean).join(" · "),
    done: ["split", "convert"].includes(job.kind) && (job.pieces || []).length
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
  // конвертацию из очереди обработки не отменить: она занимает секунды-минуты
  if (["queued", "running"].includes(job.state) && job.kind !== "convert") {
    actions.push(iconButton("close", t("Отменить"), call("cancel_edit", job.id)));
  }
  if (job.state === "done" && job.target) {
    actions.push(iconButton("play", t("Открыть"), call("open_file", job.target)),
                 iconButton("folder", t("Показать в папке"), call("show_in_folder", job.target)));
  }
  // законченную строку — убрать: иначе отменённая так и висит
  if (["done", "failed", "cancelled"].includes(job.state)) {
    actions.push(iconButton("close", t("Убрать из списка; файл останется"), call("remove_edit", job.id)));
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
  if (plain === CHAT_ONLY.quality) return t("только чат");
  if (plain === "chat-archive") return t("чат из стороннего архива logs.zonian.dev");
  if (plain === "chat-gaps") return t("пропуски чата");
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
  return parts.join(" · ") || t("запуск…");
}

// начало эфира и его склейка с записью (live/joining.py): идёт, готово или почему нет
export function joinText(job) {
  if (job.joined) return t("склеено с записью");
  const join = job.file && state.joins.get(job.file);
  if (!join) return "";
  if (join.state === "joining") return joiningText(join);
  if (join.state === "done") return t("склеено с записью");
  return t("не склеено с записью: {reason}", { reason: join.reason || "?" });
}

// начало эфира (part00) скачано, но с записью ещё не склеивалось: она идёт, склейка — после неё
export function awaitingJoin(job) {
  if (job.state !== "done" || job.joined || !/ - part00$/.test(job.fixed_target || "")) return false;
  // склейка у канала выключена — начало так и останется первой частью, ждать нечего
  const channel = state.channels.get(jobChannelId(job));
  const options = (channel && channel.options) || {};
  const joins = "join_beginning" in options ? options.join_beginning : (state.settings || {}).join_beginning;
  if (joins === false) return false;
  return !(job.file && state.joins.get(job.file));
}

// идущая склейка: этап, проценты, сколько осталось, а дописана — кого ждёт
export function joiningText(join) {
  if (join.waiting) return t("склеено, ожидание закрытия файла в «{programs}»", { programs: join.waiting });
  if (join.stage === "repack") return t("подготовка записи к склейке…");
  // видео склеено: проверка, чат, замена файлов — секунды или минута, без процентов
  if (join.stage === "finish") return t("видео склеено, проверка и замена файлов…");
  const left = joinTimeLeft(join);
  return t("склеивается с записью: {percent}%", { percent: join.percent || 0 }) + (left ? " · " + left : "");
}

// сколько примерно осталось: по скорости с первого замера этой склейки
function joinTimeLeft(join) {
  const seen = state.joinTimes.get(join.head);
  const percent = join.percent || 0;
  if (!seen || percent - seen.percent < 3 || percent >= 100) return "";
  const passed = (Date.now() - seen.at) / 1000;
  if (passed < 10) return "";
  return t("осталось {time}", { time: fmt.duration(Math.max(1, passed * (100 - percent) / (percent - seen.percent))) });
}

// размер скачанного — из последнего прогресса загрузки
export function doneSize(job) {
  const progress = job.progress || {};
  const size = job.size || progress.total || progress.downloaded;
  return size ? fmt.size(size) : "";
}

export function updateJobRow(row, job) {
  // картинки с площадки нет (начало эфира заводит сама программа) — кадр из
  // скачанного файла, а пока он качается — из идущего эфира канала. У склеенного
  // начала файл — сама запись: кадр из неё и пока докачивается чат начала
  if (job.thumbnail) setSrc(row._img, job.thumbnail);
  // чат из архива и пропуски чата — к своей записи: кадр из неё сразу, а не после конца
  else if ((job.state === "done" || job.joined || TO_RECORDING.has(job.quality)) && job.file && row._framed !== job.file) {
    row._framed = job.file;
    requestPreview(job.file, (preview) => {
      if (row._framed === job.file && preview && preview.thumbnail) setSrc(row._img, preview.thumbnail);
    });
  } else if (!row._framed) {
    const channel = state.channels.get(jobChannelId(job));
    const live = channel && channel.stream && liveThumb(channel.stream.thumbnail);
    if (live) setSrc(row._img, live);
  }
  setText(row._title, job.title || job.url);
  row._title.title = job.title || job.url;
  fillChannelLine(row._sub, jobChannelId(job), job.channel,
                  [platformName(job.platform), qualityText(job.quality), sectionText(job.section)]);

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
  // склейка начала эфира с записью — своей заметной строкой с полосой, а не
  // словами в конце «Готово · …», где её было не разглядеть
  const join = job.state === "done" && !job.joined && job.file && state.joins.get(job.file);
  const joining = join && join.state === "joining";
  if (!row._joinLine) {
    row._joinText = h("span", { class: "join-line-text" });
    row._joinBar = h("div", { class: "bar" });
    row._joinLine = h("div", { class: "join-line" }, row._joinText, h("div", { class: "progress join" }, row._joinBar));
    row._status.after(row._joinLine);
  }
  row._joinLine.hidden = !joining;
  if (joining) {
    setText(row._joinText, joiningText(join));
    row._joinBar.parentNode.className = "progress join" + (join.stage ? " indeterminate" : "");
    row._joinBar.style.width = (join.waiting ? 100 : join.percent || 0) + "%";
  }

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
      : job.chat_only ? t("скачивание чата…") : t("склейка и перепаковка…"),
    paused: t("на паузе") + (percent ? " · " + Math.floor(percent) + "%" : "")
            + " — " + t("продолжится с того же места"),
    // начало эфира скачано, а склейки ещё не было (запись идёт) — так и сказано, а не «Готово»
    done: [awaitingJoin(job) ? (recordingGoes(job) ? t("Начало эфира скачано, склейка после конца записи")
                                                 : t("Начало эфира скачано, запись обрабатывается, склейка скоро начнётся"))
             : isJoining(job) ? t("Начало эфира скачано") : t("Готово"), doneSize(job)]
      .filter(Boolean).join(" · "),
    // начало эфира: почему не взять — коротко; текст ошибки — в журнале
    failed: job.reason ? t("начало эфира не скачать: {reason}", { reason: job.reason })
                       : t("ошибка: {error}", { error: job.error || t("неизвестная") }),
    cancelled: t("отменено"),
  }[job.state] || job.state;
  // готовое — не одной строкой через точки: «Готово · размер», рядом — итог склейки
  // меткой, а примечание про чат — своей строкой ниже, приглушённо
  const joinedText = job.state === "done" && !joining ? joinText(job) : "";
  const joinFailed = joinedText && job.file && (state.joins.get(job.file) || {}).state === "failed";
  row._status.replaceChildren(statusText,
    ...(joinedText ? [h("span", { class: "status-chip" + (joinFailed ? " warn" : " ok"), text: joinedText })] : []));
  row._status.title = job.state === "done" && job.file ? job.file : job.reason && job.error ? job.error : "";
  row._status.className = "job-status" + (job.state === "failed" ? " error" : job.state === "done" ? " ok-text" : "");
  if (!row._note) {
    row._note = h("div", { class: "job-note" });
    (row._joinLine || row._status).after(row._note);
  }
  const note = job.state === "done" && job.chat_error ? t("Чат: {error}", { error: job.chat_error }) : "";
  row._note.hidden = !note;
  setText(row._note, note);

  const chatFiles = job.chat_files || {};
  const actionsKey = [job.state, job.chat_only, chatFiles.html, chatFiles.video, job.beginning_chat].join("|");
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
    // скачать чат — словами: значок чата рядом означает «открыть». У склеенного
    // начала эфира чат записи уже есть (прямой эфир) — докачивается чат самого начала
    // у загрузки, которая сама — чат к записи (из архива, пропуски), своей кнопки чата нет
    if (!TO_RECORDING.has(job.quality)
        && (job.joined ? !job.beginning_chat && chatSupported(job) : !chatFiles.html && chatSupported(job))) {
      actions.push(button(job.joined ? t("Скачать чат начала") : t("Скачать чат"),
        { kind: "ghost", icon: "download", onclick: call("download_chat", job.id),
          title: job.joined ? t("Чат начала эфира встанет в чат записи до стыка; чат прямого эфира останется") : "" }));
    }
  }
  if (["done", "cancelled", "failed", "paused"].includes(job.state)) {
    actions.push(iconButton("close", t("Убрать из списка; файл останется"), call("remove_download", job.id)));
  }
  row._actions.replaceChildren(...actions);
}
