/*
 * StreamVault, окно программы: просмотр записи с чатом — как на Twitch.
 *
 * Видео на всю высоту окна, чат справа, под видео — название и канал.
 * Кнопки свои у любого видео: шкала на всю ширину (толще при наведении,
 * время под курсором), пауза, громкость, время; справа — настройки
 * (скорость), клип, кинотеатр, во весь экран. Пока смотрят и мышь
 * не двигается, кнопки и курсор прячутся. Чат сворачивается кнопкой
 * в его шапке, разворачивается кнопкой на видео.
 */

import { bridge, button, channelName, displayTitle, english, failed, fillAvatar, fmt, h, icon, iconButton,
         openModal, platformName, setText, state, t, toast } from "./common.js";
import { openSummary } from "./summary.js";

// Чат приходит кусками по CHAT_SEGMENT секунд записи: у длинного эфира
// сотни тысяч сообщений, а нужны те, что рядом с текущим моментом.
const CHAT_SEGMENT = 120;
const CHAT_LINES = 150;       // строк чата на экране, пока он идёт за видео
const CHAT_SCROLLBACK = 1000; // и пока его листают назад
const CHAT_BACKLOG = 90;      // после перемотки — сообщения за столько секунд до неё
const CHAT_JUMP = 5;          // скачок больше этого — перемотка, а не ход видео
const WATCH_RATES = [0.5, 0.75, 1, 1.25, 1.5, 1.75, 2];
const CHAT_FONTS = [11, 12, 13, 14.5, 16, 18];   // размер шрифта чата, пикселей
const SEEK_STEP = 10;         // ←/→ — на столько секунд
const POSITION_EVERY = 10000; // как часто запоминать место просмотра, мс
const IDLE_HIDE = 2500;       // через столько мс без движения мыши кнопки прячутся
// идущая запись: смотреть на столько секунд позади её края — поток успевает
// за файлом, а чат приходит раньше видео
const LIVE_GAP = 12;
const LIVE_POLL = 8000;       // как часто спрашивать, сколько уже записано, мс
const LIVE_CHAT_EVERY = 4000; // как часто дочитывать свежий кусок чата, мс
const STORYBOARD_RETRY = 5000; // кадры для шкалы ещё делаются — спросить снова через столько мс
const PEAK_LEAD = 15;         // всплеск чата — реакция: смотреть с 15 секунд до него
const SVG = "http://www.w3.org/2000/svg";

// запись канала, которая идёт прямо сейчас: тот же проигрыватель, но часть
// растёт, «К эфиру» возвращает к её краю, а новая часть после обрыва включается сама
export function openLive(cid) {
  return bridge.call("watch_live", cid)
    .then((result) => openWatch(result.item, { live: result, cid: cid }))
    .catch(failed);
}

// как этот зритель смотрит записи: скорость, громкость, чат, кинотеатр.
// Место просмотра хранит программа (save_position), а это — мелочи окна
function watchPrefs() {
  const prefs = { rate: 1, chat: true, font: 2, volume: 1, muted: false, theatre: false, timestamps: true,
                  activity: true };
  try { Object.assign(prefs, JSON.parse(localStorage.getItem("watch-prefs") || "{}")); } catch (error) { /* нет памяти — по умолчанию */ }
  if (!WATCH_RATES.includes(prefs.rate)) prefs.rate = 1;
  if (!(prefs.font >= 0 && prefs.font < CHAT_FONTS.length)) prefs.font = 2;
  if (!(prefs.volume >= 0 && prefs.volume <= 1)) prefs.volume = 1;
  return prefs;
}

function saveWatchPrefs(prefs) {
  try { localStorage.setItem("watch-prefs", JSON.stringify(prefs)); } catch (error) { /* обойдёмся */ }
}

function rateLabel(rate) {
  return rate === 1 ? t("обычная") : String(rate).replace(".", english() ? "." : ",") + "×";
}

// всплывающее меню над кнопкой: закрывается щелчком мимо и Esc
function popover(anchor, build) {
  const menu = h("div", { class: "watch-menu" });
  const close = () => {
    menu.remove();
    document.removeEventListener("mousedown", outside, true);
    document.removeEventListener("keydown", escape, true);
  };
  const outside = (event) => { if (!menu.contains(event.target) && !anchor.contains(event.target)) close(); };
  const escape = (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      close();
    }
  };
  build(menu, close);
  anchor.parentNode.append(menu);
  document.addEventListener("mousedown", outside, true);
  document.addEventListener("keydown", escape, true);
  return close;
}

// item — запись из библиотеки; options.autoplay — следующая часть эфира: сразу смотреть;
// options.live — ответ watch_live: запись ещё идёт (options.cid — чья)
export function openWatch(item, options) {
  options = options || {};
  let closed = false;
  const prefs = watchPrefs();
  const video = h("video", { class: "watch-video", preload: "auto" });
  video.volume = prefs.volume;
  video.muted = prefs.muted;
  const modal = openModal({ kind: "watch", onClose: () => {
    rememberPosition();
    closed = true;
    clearInterval(positionTimer);
    clearInterval(liveTimer);
    clearTimeout(idleTimer);
    document.removeEventListener("keydown", onKey);
    video.pause();
    video.removeAttribute("src");
    video.load();             // отпустить файл и соединение
  } });
  const title = displayTitle(item.title) || item.title;
  const sub = [channelName(item.channel_id, item.channel), platformName(item.platform), fmt.date(item.timestamp)]
    .filter(Boolean).join(" · ");
  const external = () => {
    video.pause();
    bridge.call("open_file", item.path).catch(failed);
  };

  // ---------------------------------------------------------------- видео и его кнопки

  const problem = h("div", { class: "watch-problem" });
  problem.hidden = true;
  const bigPlay = h("div", { class: "watch-bigplay" }, icon("play"));
  const expandChat = iconButton("expand", t("Развернуть чат"), () => showChat(true));
  expandChat.classList.add("watch-expand");
  const top = h("div", { class: "watch-top" },
    h("div", { class: "watch-top-title", text: title, title: item.name }),
    h("div", { class: "watch-top-sub", text: sub }));

  // шкала: заполнено до текущего места, светлее — загруженное, под курсором — время
  const seekBuffer = h("div", { class: "watch-seek-buffer" });
  const seekFill = h("div", { class: "watch-seek-fill" });
  const seekThumb = h("div", { class: "watch-seek-thumb" });
  const seekTip = h("div", { class: "watch-seek-tip" });
  // кадр записи над шкалой — из раскадровки, когда она готова
  const seekPreview = h("div", { class: "watch-seek-preview" });
  seekPreview.hidden = true;
  // пропуски в записи — пометки на шкале
  const seekGaps = h("div", { class: "watch-seek-gaps" });
  const seek = h("div", { class: "watch-seek", role: "slider", "aria-label": t("Перемотка"), tabindex: "-1" },
    h("div", { class: "watch-seek-track" }, seekBuffer, seekFill, seekGaps), seekThumb, seekPreview, seekTip);
  // график чата над шкалой: сколько сообщений в каждом отрезке; всплески — щелчком
  const activityBars = document.createElementNS(SVG, "svg");
  activityBars.setAttribute("class", "watch-activity-bars");
  activityBars.setAttribute("preserveAspectRatio", "none");
  activityBars.setAttribute("aria-hidden", "true");
  const activityPeaks = h("div", { class: "watch-activity-peaks" });
  const activityGraph = h("div", { class: "watch-activity" }, activityBars, activityPeaks);
  activityGraph.hidden = true;

  const play = iconButton("play", t("Смотреть"), () => togglePlay());
  const mute = iconButton("volume", t("Без звука"), () => {
    video.muted = !video.muted;
    if (!video.muted && !video.volume) video.volume = 0.5;
  });
  const volume = h("input", { type: "range", class: "watch-volume", min: 0, max: 1, step: 0.02,
                              "aria-label": t("Громкость") });
  volume.value = String(prefs.muted ? 0 : prefs.volume);
  volume.addEventListener("input", () => {
    video.volume = Number(volume.value);
    video.muted = !video.volume;
  });
  const time = h("span", { class: "watch-time" });
  // идущая запись: у края — «В ЭФИРЕ», перемотали назад — «К ЭФИРУ»
  const liveBadge = h("button", { type: "button", class: "watch-live", title: t("К тому, что идёт сейчас") });
  liveBadge.hidden = true;
  liveBadge.addEventListener("click", () => seekTo(Math.max(0, duration - LIVE_GAP), true));
  const rateBadge = h("span", { class: "watch-rate-badge" });
  const settingsButton = iconButton("gear", t("Настройки"), () => openSettingsMenu());
  const clipButton = iconButton("scissors", t("Клип (клавиши [ и ])"), () => {
    clipBar.hidden = !clipBar.hidden;
    refreshClip();
  });
  const theatreButton = iconButton("theatre", t("Режим кинотеатра"), () => setTheatre(!prefs.theatre));
  const fullButton = iconButton("fullscreen", t("Во весь экран (F)"), () => toggleFullscreen());
  const controls = h("div", { class: "watch-controls" },
    activityGraph, seek,
    h("div", { class: "watch-buttons" },
      play, h("div", { class: "watch-volume-group" }, mute, volume), liveBadge, time,
      h("span", { class: "spacer" }),
      rateBadge, h("div", { class: "watch-menu-anchor" }, settingsButton), clipButton, theatreButton, fullButton));
  const stage = h("div", { class: "watch-stage paused" }, video, bigPlay, top, expandChat, controls, problem);

  // ---------------------------------------------------------------- клип

  // клип из записи: отметить начало и конец, ffmpeg режет без перекодирования
  let clipStart = null, clipEnd = null;
  const clipStartText = h("span", { class: "watch-clip-time" });
  const clipEndText = h("span", { class: "watch-clip-time" });
  const clipChat = h("input", { type: "checkbox" });
  clipChat.checked = true;
  const clipChatLabel = h("label", { class: "switch", title: t("Рядом с клипом — чат этого отрезка") },
    clipChat, h("span", { class: "track" }), t("с чатом"));
  const clipSave = button(t("Сохранить клип"), { kind: "primary", icon: "scissors", onclick: () => saveClip() });
  const clipBar = h("div", { class: "watch-clip" },
    button(t("Начало"), { kind: "ghost", title: t("Начало клипа — это место записи (клавиша [)"),
                          onclick: () => markClip("start") }), clipStartText,
    button(t("Конец"), { kind: "ghost", title: t("Конец клипа — это место записи (клавиша ])"),
                         onclick: () => markClip("end") }), clipEndText,
    clipChatLabel, h("span", { class: "spacer" }),
    h("span", { class: "muted watch-clip-note",
                text: t("Режется без перекодирования, по ключевым кадрам: начало может сдвинуться на пару секунд раньше.") }),
    clipSave, iconButton("close", t("Закрыть"), () => { clipBar.hidden = true; }));
  clipBar.hidden = true;

  // ---------------------------------------------------------------- под видео: канал и название

  let parts = [];
  const partSelect = h("select", { class: "watch-parts", title: t("Части эфира") });
  partSelect.hidden = true;
  partSelect.addEventListener("change", () => openPart(Number(partSelect.value), true));
  const avatar = h("span", { class: "avatar big" });
  fillAvatar(avatar, item.channel_id, item.platform);
  const info = h("div", { class: "watch-info" },
    avatar,
    h("div", { class: "watch-info-text" },
      h("div", { class: "watch-info-channel", text: channelName(item.channel_id, item.channel) }),
      h("h2", { class: "watch-info-title", text: title, title: item.name }),
      h("div", { class: "muted", text: [platformName(item.platform), fmt.date(item.timestamp)].filter(Boolean).join(" · ") })),
    partSelect,
    // сводка эфира: чат, всплески, пропуски — по всем частям
    options.live ? null : button(t("Сводка"), { kind: "ghost", icon: "stats", title: t("Сводка эфира"),
      onclick: () => openSummary(item, { jump: (part, seconds) => jumpToPart(part, seconds) }) }),
    button(t("Открыть в проигрывателе"), { kind: "ghost", icon: "external", onclick: external }));
  const main = h("div", { class: "watch-main" }, stage, clipBar, info);

  // ---------------------------------------------------------------- чат

  const status = h("div", { class: "watch-chat-status", text: t("загружаю чат…") });
  const list = h("div", { class: "watch-chat-list" });
  const resume = h("button", { type: "button", class: "watch-resume" }, t("Чат приостановлен из-за прокрутки"));
  resume.addEventListener("click", () => {
    follow = true;
    resume.hidden = true;
    trim();
    list.scrollTop = list.scrollHeight;
  });
  resume.hidden = true;
  // поиск по чату: совпадения списком, щелчок — к этому месту записи
  const searchInput = h("input", { type: "search", class: "watch-search", placeholder: t("Поиск по чату"),
                                   spellcheck: "false" });
  const searchRow = h("div", { class: "watch-search-row" }, searchInput);
  searchRow.hidden = true;
  const found = h("div", { class: "watch-chat-list watch-found" });
  found.hidden = true;
  const chatSettingsButton = iconButton("gear", t("Настройки чата"), () => openChatMenu());
  const chatPanel = h("aside", { class: "watch-chat" },
    h("div", { class: "watch-chat-head" },
      iconButton("collapse", t("Свернуть чат"), () => showChat(false)),
      h("span", { class: "watch-chat-title", text: t("Чат") }),
      iconButton("search", t("Поиск по чату"), () => {
        searchRow.hidden = !searchRow.hidden;
        if (!searchRow.hidden) searchInput.focus();
        else { searchInput.value = ""; search(); }
      }),
      h("div", { class: "watch-menu-anchor" }, chatSettingsButton)),
    searchRow, status, list, found, resume);

  const body = h("div", { class: "watch-body" }, main, chatPanel);
  modal.body.append(body);

  // ---------------------------------------------------------------- состояние

  let chat = null;            // {id, count, images}
  // Запись в .ts ffmpeg перепаковывает на лету с места base: время
  // в записи — base плюс время потока; перемотка — новый поток.
  let remux = false, base = 0;
  const position = () => base + video.currentTime;
  let local = "";             // начало адресов локального сервера программы
  let shift = 0;              // секунд: больше нуля — чат позже
  let shownUntil = null;      // до какого времени чат показан; null — показать заново
  let live = false;           // запись ещё идёт: часть растёт
  let liveNext = false;       // часть кончилась, эфир идёт дальше новой — перейти к ней в конце
  let liveStalled = false;    // поток дошёл до края записи — продолжить, когда она подрастёт
  let liveTimer = null;
  const fetchedAt = new Map(); // номер куска чата -> когда его спрашивали
  let follow = true;          // чат прокручивается за новыми сообщениями
  const segments = new Map(); // номер куска -> сообщения (null — ещё грузится)
  let source = "", duration = 0, lastTime = 0, recoveries = 0;

  // ---------------------------------------------------------------- кнопки видео

  function togglePlay() {
    if (video.paused) video.play().catch(() => {});
    else video.pause();
  }

  let dragging = null;        // время, куда тянут шкалу

  function seekAt(event) {
    const box = seek.getBoundingClientRect();
    const share = Math.max(0, Math.min(1, (event.clientX - box.left) / box.width));
    return { share: share, seconds: share * duration };
  }

  seek.addEventListener("pointermove", (event) => {
    if (!duration) return;
    const at = seekAt(event);
    const width = seek.getBoundingClientRect().width;
    const shown = showPreview(at.seconds);
    // кадр и подпись не вылезают за края шкалы
    const half = shown ? seekPreview.offsetWidth / 2 : 30;
    const x = Math.max(half, Math.min(width - half, at.share * width));
    seekPreview.style.left = seekTip.style.left = x + "px";
    const gap = gapAt(at.seconds);
    setText(seekTip, fmt.duration(at.seconds) + (gap ? " · " + gapText(gap) : ""));
    if (dragging !== null) {
      dragging = at.seconds;
      update();
    }
  });
  seek.addEventListener("pointerleave", () => { seekPreview.hidden = true; });
  seek.addEventListener("pointerdown", (event) => {
    if (!duration || event.button !== 0) return;
    seek.setPointerCapture(event.pointerId);
    seek.classList.add("dragging");
    dragging = seekAt(event).seconds;
    update();
  });
  seek.addEventListener("pointerup", () => {
    if (dragging === null) return;
    const seconds = dragging;
    dragging = null;
    seek.classList.remove("dragging");
    jump(seconds, !video.paused || video.autoplay);
  });

  function update() {
    const now = dragging !== null ? dragging : position();
    // длину идущей записи программа узнаёт раз в несколько секунд — видео бывает чуть впереди
    const total = Math.max(duration, now);
    const share = total ? Math.min(1, now / total) : 0;
    seekFill.style.width = (share * 100) + "%";
    seekThumb.style.left = (share * 100) + "%";
    let buffered = 0;
    if (video.buffered.length) buffered = base + video.buffered.end(video.buffered.length - 1);
    seekBuffer.style.width = (total ? Math.min(1, buffered / total) * 100 : 0) + "%";
    setText(time, fmt.duration(now) + " / " + fmt.duration(total));
    play.replaceChildren(icon(video.paused ? "play" : "pause"));
    play.title = play.ariaLabel = video.paused ? t("Смотреть (пробел)") : t("Пауза (пробел)");
    const silent = video.muted || !video.volume;
    mute.replaceChildren(icon(silent ? "muted" : "volume"));
    mute.title = mute.ariaLabel = silent ? t("Со звуком (M)") : t("Без звука (M)");
    if (document.activeElement !== volume) volume.value = String(video.muted ? 0 : video.volume);
    stage.classList.toggle("paused", video.paused);
    if (live) {
      const atEdge = duration - now < LIVE_GAP + 8;
      liveBadge.classList.toggle("behind", !atEdge);
      setText(liveBadge, atEdge ? t("В эфире") : t("К эфиру"));
    }
    setText(rateBadge, prefs.rate === 1 ? "" : rateLabel(prefs.rate));
  }
  for (const name of ["timeupdate", "play", "pause", "volumechange", "progress", "durationchange"]) {
    video.addEventListener(name, update);
  }
  video.addEventListener("volumechange", () => {
    prefs.volume = video.volume;
    prefs.muted = video.muted;
    saveWatchPrefs(prefs);
  });
  // щелчок — пауза, двойной — во весь экран, как на Twitch
  video.addEventListener("click", togglePlay);
  bigPlay.addEventListener("click", togglePlay);
  video.addEventListener("dblclick", toggleFullscreen);

  // кнопки и курсор прячутся, пока смотрят и мышь стоит
  let idleTimer = null;
  function wake() {
    stage.classList.add("active");
    clearTimeout(idleTimer);
    idleTimer = setTimeout(() => stage.classList.remove("active"), IDLE_HIDE);
  }
  stage.addEventListener("mousemove", wake);
  stage.addEventListener("mouseleave", () => {
    clearTimeout(idleTimer);
    stage.classList.remove("active");
  });

  function openSettingsMenu() {
    popover(settingsButton, (menu, close) => {
      menu.append(h("div", { class: "watch-menu-title", text: t("Скорость воспроизведения") }));
      for (const rate of WATCH_RATES) {
        const item = h("button", { type: "button", class: "watch-menu-item" + (rate === prefs.rate ? " checked" : "") },
          icon("check"), rateLabel(rate));
        item.addEventListener("click", () => {
          setRate(rate);
          close();
        });
        menu.append(item);
      }
      // график чата над шкалой — по желанию
      if (chat) {
        const graph = h("input", { type: "checkbox" });
        graph.checked = prefs.activity;
        graph.addEventListener("change", () => {
          prefs.activity = graph.checked;
          saveWatchPrefs(prefs);
          drawActivity();
        });
        menu.append(h("label", { class: "switch watch-menu-row" }, graph, h("span", { class: "track" }), t("График чата")));
      }
    });
  }

  function openChatMenu() {
    popover(chatSettingsButton, (menu) => {
      const row = (label, ...controls) => h("div", { class: "watch-menu-row" }, h("span", { text: label }), ...controls);
      const fontValue = h("span", { class: "watch-menu-value" });
      const shiftValue = h("span", { class: "watch-menu-value" });
      const stamps = h("input", { type: "checkbox" });
      stamps.checked = prefs.timestamps;
      stamps.addEventListener("change", () => {
        prefs.timestamps = stamps.checked;
        saveWatchPrefs(prefs);
        shownUntil = null;
        tick();
      });
      const show = () => {
        setText(fontValue, CHAT_FONTS[prefs.font] + " px");
        setText(shiftValue, t("{seconds} с", { seconds: (shift > 0 ? "+" : "") + shift }));
      };
      const step = (label, title, change) => button(label, { kind: "ghost", title: title, onclick: () => {
        change();
        show();
      } });
      menu.append(
        h("div", { class: "watch-menu-title", text: t("Настройки чата") }),
        row(t("Размер шрифта"),
          step("A−", t("Мельче"), () => { prefs.font = Math.max(0, prefs.font - 1); saveWatchPrefs(prefs); applyFont(); }),
          fontValue,
          step("A+", t("Крупнее"), () => { prefs.font = Math.min(CHAT_FONTS.length - 1, prefs.font + 1); saveWatchPrefs(prefs); applyFont(); })),
        row(t("Сдвиг чата"),
          step("−1", t("Чат на секунду раньше"), () => moveChat(-1)), shiftValue,
          step("+1", t("Чат на секунду позже"), () => moveChat(1))),
        h("div", { class: "watch-menu-hint", text: t("Если чат спешит или отстаёт от видео") }),
        h("label", { class: "switch watch-menu-row" }, stamps, h("span", { class: "track" }), t("Время сообщений")));
      show();
    });
  }

  function moveChat(delta) {
    shift += delta;
    shownUntil = null;
    tick();
  }

  // ---------------------------------------------------------------- клип

  function markClip(which) {
    if (which === "start") clipStart = position(); else clipEnd = position();
    clipBar.hidden = false;
    refreshClip();
  }

  function refreshClip() {
    setText(clipStartText, clipStart === null ? "—" : fmt.duration(clipStart));
    setText(clipEndText, clipEnd === null ? "—" : fmt.duration(clipEnd));
    clipSave.disabled = clipStart === null || clipEnd === null || clipEnd - clipStart < 1;
    clipSave.title = clipStart !== null && clipEnd !== null && clipEnd <= clipStart
      ? t("Конец клипа должен быть позже начала") : "";
    clipChatLabel.hidden = !chat;
    clipButton.classList.toggle("on", !clipBar.hidden);
  }

  function saveClip() {
    clipSave.disabled = true;
    bridge.call("cut_clip", item.path, clipStart, clipEnd, Boolean(chat) && clipChat.checked)
      .then(() => {
        toast(t("Вырезаю клип — он ляжет рядом с записью"), "ok", "#downloads");
        clipStart = clipEnd = null;
        clipBar.hidden = true;
        refreshClip();
      })
      .catch((error) => {
        refreshClip();
        failed(error);
      });
  }

  // ---------------------------------------------------------------- вид

  function applyFont() { list.style.fontSize = found.style.fontSize = CHAT_FONTS[prefs.font] + "px"; }

  function showChat(visible) {
    prefs.chat = visible;
    saveWatchPrefs(prefs);
    body.classList.toggle("no-chat", !visible);
    expandChat.hidden = visible;
    if (visible) { shownUntil = null; tick(); }
  }

  // кинотеатр: видео и чат на всё окно, без полосы с названием
  function setTheatre(on) {
    prefs.theatre = on;
    saveWatchPrefs(prefs);
    body.classList.toggle("theatre", on);
    theatreButton.classList.toggle("on", on);
    theatreButton.title = theatreButton.ariaLabel = on ? t("Выйти из режима кинотеатра") : t("Режим кинотеатра");
  }

  // скорость остаётся и после перемотки .ts (новый поток) и в следующей части
  function setRate(rate) {
    prefs.rate = rate;
    saveWatchPrefs(prefs);
    video.defaultPlaybackRate = video.playbackRate = rate;
    update();
  }
  video.addEventListener("loadedmetadata", () => { video.playbackRate = prefs.rate; });

  function toggleFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen();
    else stage.requestFullscreen().catch(() => {});
  }

  // ---------------------------------------------------------------- чат по ходу видео

  function imageSrc(ref) {
    // у идущей записи смайлы и значки — прямо адресами площадок, а не номерами
    const url = typeof ref === "string" ? ref : chat.images[ref];
    return typeof url === "string" && (url.startsWith(local) || /^https:\/\//i.test(url)) ? url : "";
  }

  function chatLine(message) {
    const [offset, name, color, badges, parts] = message;
    const line = h("div", { class: "chat-line" });
    if (prefs.timestamps) line.append(h("span", { class: "chat-time", text: fmt.duration(offset) }));
    for (const index of badges) {
      const src = imageSrc(index);
      if (src) line.append(h("img", { class: "chat-badge", src: src, alt: "" }));
    }
    const nameEl = h("span", { class: "chat-name", text: name });
    if (/^#[0-9a-f]{6}$/i.test(color)) nameEl.style.color = color;
    line.append(nameEl, ": ");
    for (const part of parts) {
      if (typeof part === "string") {
        line.append(part);
        continue;
      }
      const src = imageSrc(part[0]);
      line.append(src ? h("img", { class: "chat-emote", src: src, alt: part[1], title: part[1], onload: stick })
                      : part[1]);
    }
    return line;
  }

  // смайл догрузился и сделал строку выше — чат остаётся внизу
  function stick() {
    if (follow) list.scrollTop = list.scrollHeight;
  }

  function segment(index) {
    if (index < 0) return [];
    if (segments.has(index)) {
      // у идущей записи последний кусок чата дописывается — его переспрашиваем
      if (live && segments.get(index) !== null && (index + 1) * CHAT_SEGMENT > duration - CHAT_JUMP
          && Date.now() - (fetchedAt.get(index) || 0) > LIVE_CHAT_EVERY) fetchSegment(index);
      return segments.get(index);
    }
    segments.set(index, null);
    fetchSegment(index);
    return null;
  }

  function fetchSegment(index) {
    fetchedAt.set(index, Date.now());
    bridge.call("chat_replay", chat.id, index * CHAT_SEGMENT, (index + 1) * CHAT_SEGMENT)
      .then((messages) => {
        segments.set(index, messages);
        tick();
      })
      .catch((error) => {
        if (segments.get(index) === null) segments.delete(index);
        setText(status, t("чат не загрузился: {error}", { error: error.message || error }));
      });
  }

  // сообщения после from и до to включительно; null — какой-то кусок ещё грузится
  function messagesBetween(from, to) {
    const result = [];
    for (let index = Math.floor(Math.max(0, from) / CHAT_SEGMENT); index <= Math.floor(to / CHAT_SEGMENT); index++) {
      const messages = segment(index);
      if (messages === null) return null;
      for (const message of messages) {
        if (message[0] > from && message[0] <= to) result.push(message);
      }
    }
    return result;
  }

  function trim() {
    const keep = follow ? CHAT_LINES : CHAT_SCROLLBACK;
    while (list.children.length > keep) list.firstChild.remove();
  }

  function tick() {
    if (!chat || closed) return;
    const now = position() - shift;
    segment(Math.floor((now + 30) / CHAT_SEGMENT));     // следующий кусок — заранее
    if (shownUntil === null || now < shownUntil || now - shownUntil > CHAT_JUMP) {
      const messages = messagesBetween(now - CHAT_BACKLOG, now);
      if (messages === null) return;
      list.replaceChildren(...messages.slice(-CHAT_LINES).map(chatLine));
      follow = true;
      resume.hidden = true;
    } else {
      const messages = messagesBetween(shownUntil, now);
      if (messages === null) return;
      if (messages.length) {
        list.append(...messages.map(chatLine));
        trim();
      }
    }
    shownUntil = now;
    if (follow) list.scrollTop = list.scrollHeight;
  }

  list.addEventListener("scroll", () => {
    // листают назад — чат не дёргается вниз, пока не вернутся
    const atBottom = list.scrollHeight - list.scrollTop - list.clientHeight < 40;
    if (atBottom !== follow) {
      follow = atBottom;
      resume.hidden = follow;
    }
  });
  for (const name of ["timeupdate", "seeked", "seeking", "play"]) video.addEventListener(name, tick);

  // ---------------------------------------------------------------- сбои видео

  video.addEventListener("loadedmetadata", () => {
    if (!remux && isFinite(video.duration)) duration = video.duration;
    update();
    // у mp4 длительность видна только теперь — по ней график и пометки
    drawGaps();
    loadActivity();
  });
  video.addEventListener("playing", () => { recoveries = 0; });
  for (const name of ["timeupdate", "seeking"]) {
    video.addEventListener(name, () => { if (!video.error) lastTime = position(); });
  }
  video.addEventListener("error", () => {
    if (closed) return;
    // запись эфира бывает оборвана на полукадре: декодер спотыкается,
    // а смотреть файл можно — открыть его заново чуть дальше сбоя,
    // а у самого конца — за пару секунд до него и на паузе
    if (duration && recoveries < 3) {
      recoveries++;
      const nearEnd = lastTime > duration - 3;
      if (remux) {
        seekTo(nearEnd ? duration - 3 : lastTime + 2, !nearEnd);
        return;
      }
      video.autoplay = !nearEnd;
      video.src = source;
      video.currentTime = Math.max(0, nearEnd ? duration - 3 : lastTime + 2);
      return;
    }
    const ts = /\.ts$/i.test(item.path);
    const convertible = ts && item.kind === "record" && !state.converting.has(item.path);
    problem.replaceChildren(
      h("div", { text: ts ? t("Запись в .ts окно не проигрывает: переведи её в mp4 или открой в проигрывателе.")
                          : t("Это видео окно проиграть не может — открой его в проигрывателе.") }),
      h("div", { class: "toolbar" },
        convertible ? button(t("Конвертировать в mp4"), { kind: "primary", icon: "convert", onclick: () => {
          state.converting.add(item.path);
          bridge.call("convert_recording", item.path)
            .then(() => {
              toast(t("Конвертирую в mp4: {name}", { name: item.name }), "ok");
              modal.close();
            })
            .catch((error) => {
              state.converting.delete(item.path);
              failed(error);
            });
        } }) : null,
        button(t("Открыть в проигрывателе"), { kind: convertible ? "" : "primary", icon: "external",
                                               onclick: external })));
    problem.hidden = false;
  });

  // ---------------------------------------------------------------- кадры, пропуски и график чата

  let board = null;           // раскадровка: {interval, columns, rows, sheets, width, height}
  let gaps = [];              // пропуски в записи: [{at, length, kept}]

  function loadStoryboard(attempt) {
    if (closed || live) return;
    bridge.call("storyboard", item.path).then((answer) => {
      if (closed) return;
      if (answer.state === "working" && attempt < 120) {
        setTimeout(() => loadStoryboard(attempt + 1), STORYBOARD_RETRY);
        return;
      }
      if (answer.state !== "ready" || !answer.sheets.length) return;
      // размер кадра — по первому листу: листы одинаковые, кадры в них по сетке
      const first = new Image();
      first.addEventListener("load", () => {
        if (closed) return;
        board = Object.assign({}, answer, { width: first.naturalWidth / answer.columns,
                                            height: first.naturalHeight / answer.rows });
        seekPreview.style.width = board.width + "px";
        seekPreview.style.height = board.height + "px";
        // остальные листы — заранее, чтобы кадр под курсором не мигал
        for (const sheet of answer.sheets.slice(1)) new Image().src = sheet;
      });
      first.src = answer.sheets[0];
    }).catch(() => {});
  }

  // кадр для места записи; false — раскадровки нет
  function showPreview(seconds) {
    if (!board) {
      seekPreview.hidden = true;
      return false;
    }
    const perSheet = board.columns * board.rows;
    const index = Math.min(Math.floor(seconds / board.interval), board.sheets.length * perSheet - 1);
    const sheet = Math.floor(index / perSheet), within = index % perSheet;
    seekPreview.style.backgroundImage = "url(\"" + board.sheets[sheet] + "\")";
    seekPreview.style.backgroundSize = (board.width * board.columns) + "px " + (board.height * board.rows) + "px";
    seekPreview.style.backgroundPosition = -(within % board.columns) * board.width + "px "
      + -Math.floor(within / board.columns) * board.height + "px";
    seekPreview.hidden = false;
    return true;
  }

  // пропуск: оставшийся во времени — полосой, склеенный — чертой
  function drawGaps() {
    seekGaps.replaceChildren();
    if (!duration) return;
    for (const gap of gaps) {
      const mark = h("span", { class: "watch-gap" + (gap.kept && gap.length ? "" : " cut") });
      mark.style.left = Math.min(100, gap.at / duration * 100) + "%";
      if (gap.kept && gap.length) mark.style.width = Math.min(100, gap.length / duration * 100) + "%";
      seekGaps.append(mark);
    }
  }

  function gapAt(seconds) {
    const near = duration / 200;  // черту склеенного пропуска легко не попасть — с запасом
    return gaps.find((gap) => gap.kept && gap.length ? seconds >= gap.at && seconds <= gap.at + gap.length
                                                     : Math.abs(seconds - gap.at) <= near) || null;
  }

  function gapText(gap) {
    return gap.length ? t("пропуск в записи: {length}", { length: fmt.duration(gap.length) })
                      : t("пропуск в записи");
  }

  let activity = null;        // {bucket, counts, peaks}
  let activityAt = 0;

  function loadActivity() {
    if (!chat || closed || !duration) return;
    // идущая запись растёт — график обновляется, но не чаще раза в LIVE_POLL
    if (activity && (!live || Date.now() - activityAt < LIVE_POLL)) return;
    activityAt = Date.now();
    bridge.call("chat_activity", chat.id, duration).then((answer) => {
      if (closed) return;
      activity = answer;
      drawActivity();
    }).catch(() => {});
  }

  function drawActivity() {
    activityGraph.hidden = !activity || !prefs.activity || !activity.counts.some(Boolean);
    if (activityGraph.hidden) return;
    const counts = activity.counts, top = Math.max(...counts);
    // в масштабе шкалы: последний столбик бывает длиннее записи — он обрезается
    const total = duration || counts.length * activity.bucket;
    // столбики во всю высоту графика: самый частый отрезок — до верха
    activityBars.setAttribute("viewBox", "0 0 " + counts.length + " 100");
    activityBars.style.width = Math.min(100, counts.length * activity.bucket / total * 100) + "%";
    let path = "";
    counts.forEach((count, index) => {
      if (count) path += "M" + index + " 100V" + (100 - count / top * 100).toFixed(1) + "h0.85V100z";
    });
    const shape = document.createElementNS(SVG, "path");
    shape.setAttribute("d", path);
    activityBars.replaceChildren(shape);
    activityPeaks.replaceChildren(...activity.peaks.map((peak) => {
      const at = peak * activity.bucket;
      const mark = h("button", { type: "button", class: "watch-peak",
        title: t("Всплеск чата: {count} за {length} — смотреть", { count: fmt.counted(counts[peak],
                 t("сообщение|сообщения|сообщений")), length: fmt.duration(activity.bucket) }) });
      mark.style.left = Math.min(100, (at + activity.bucket / 2) / total * 100) + "%";
      mark.addEventListener("click", () => jump(Math.max(0, at - PEAK_LEAD), true));
      return mark;
    }));
  }

  // из сводки: место в этой части — перемотка, в другой — открыть её с этого места
  function jumpToPart(path, seconds) {
    if (path === item.path) {
      jump(seconds, true);
      return;
    }
    const part = parts.find((candidate) => candidate.path === path);
    if (part) openPart(part.part, true, seconds);
  }

  // ---------------------------------------------------------------- перемотка и клавиши

  // перейти к месту записи: у .ts — новый поток, у прочего — сам элемент
  function jump(seconds, play) {
    seconds = Math.max(0, duration ? Math.min(seconds, duration - 1) : seconds);
    if (remux) {
      seekTo(seconds, play === undefined ? !video.paused : play);
      return;
    }
    video.currentTime = seconds;
    if (play) video.play().catch(() => {});
  }

  // перемотка перепакованной записи: поток с нового места
  function seekTo(seconds, play) {
    base = Math.max(0, Math.min(seconds, duration - 1));
    video.autoplay = play !== false;
    video.src = source + "?start=" + base.toFixed(1);
    shownUntil = null;
    update();
  }

  // клавиши как у проигрывателей площадок: пробел, ←/→, F, M — и для .ts
  function onKey(event) {
    if (closed || event.ctrlKey || event.altKey || event.metaKey) return;
    // поверх проигрывателя открыто другое окно (сводка) — клавиши его
    const backdrop = modal.body.parentNode;
    if (backdrop.parentNode && backdrop.parentNode.lastElementChild !== backdrop) return;
    const target = event.target;
    if (target && (["INPUT", "SELECT", "TEXTAREA"].includes(target.tagName) || target.isContentEditable)) return;
    const key = event.key.toLowerCase();
    if (key === " " || key === "k" || key === "л") togglePlay();
    else if (key === "arrowleft") jump(position() - SEEK_STEP);
    else if (key === "arrowright") jump(position() + SEEK_STEP);
    else if (key === "f" || key === "а") toggleFullscreen();
    else if (key === "m" || key === "ь") video.muted = !video.muted;
    else if (key === "[" || key === "х") markClip("start");
    else if (key === "]" || key === "ъ") markClip("end");
    else return;
    event.preventDefault();
    wake();
  }
  document.addEventListener("keydown", onKey);

  // место просмотра хранит программа: по ходу, на паузе и при закрытии
  let positionTimer = null, savedAt = -1, played = false;
  // пока запись не пошла, помнить нечего: иначе закрытое окно стёрло бы прошлое место
  video.addEventListener("playing", () => { played = true; });
  function rememberPosition() {
    // идущую запись помнить незачем: эта часть станет другим файлом
    if (closed || !source || !played || live || options.live) return;
    const seconds = position();
    if (Math.abs(seconds - savedAt) < 1) return;
    savedAt = seconds;
    bridge.call("save_position", item.path, seconds, duration || null).catch(() => {});
  }
  video.addEventListener("pause", rememberPosition);

  // ---------------------------------------------------------------- запуск

  (options.live ? Promise.resolve(options.live) : bridge.call("watch", item.path)).then((result) => {
    if (closed) return;
    const address = /^(http:\/\/127\.0\.0\.1:\d+\/[\w-]+\/)(file|remux)\//.exec(result.video);
    if (!address) throw new Error(t("непонятный ответ программы"));
    local = address[1];
    source = result.video;
    if (result.remux) {
      remux = true;
      duration = result.duration;
    }
    parts = result.parts || [];
    if (parts.length > 1) {
      partSelect.replaceChildren(...parts.map((part) => h("option", { value: String(part.part),
        text: t("часть {part} из {count}", { part: part.part, count: parts.length }) })));
      partSelect.value = String((parts.find((part) => part.path === item.path) || parts[0]).part);
      partSelect.hidden = false;
    }
    video.defaultPlaybackRate = prefs.rate;
    const from = result.position;
    gaps = result.gaps || [];
    drawGaps();
    if (!result.live) loadStoryboard(0);
    if (result.live) {
      // идущая запись — с её края; назад — шкалой, вперёд — «К эфиру»
      live = true;
      liveBadge.hidden = false;
      seekTo(Math.max(0, duration - LIVE_GAP), true);
      liveTimer = setInterval(pollLive, LIVE_POLL);
    } else if (options.start !== undefined) {
      startAt(options.start);       // из поиска по чатам и сводки — сразу с нужного места
    } else if (from && !options.autoplay) {
      offerResume(from);
    } else {
      video.autoplay = true;
      video.src = source;
    }
    if (!result.live) positionTimer = setInterval(() => { if (!video.paused) rememberPosition(); }, POSITION_EVERY);
    update();
    chat = result.chat;
    // в чате идущей записи сообщения ещё будут
    if (!chat || (!chat.count && !chat.live)) {
      chat = null;
      searchInput.disabled = true;
      setText(status, result.live ? t("Чат этой записи не пишется.") : t("В чате этой записи нет сообщений."));
      refreshClip();
      return;
    }
    setText(status, chat.live ? t("чат пишется вместе с записью")
                              : fmt.counted(chat.count, t("сообщение|сообщения|сообщений")));
    refreshClip();
    tick();
    loadActivity();
  }).catch((error) => {
    if (!closed) {
      modal.close();
      failed(error);
    }
  });

  // в прошлый раз остановились посередине — продолжить или с начала
  function offerResume(from) {
    const prompt = h("div", { class: "watch-offer" },
      h("div", { text: t("Просмотр остановлен на {time}", { time: fmt.duration(from) }) }),
      h("div", { class: "toolbar" },
        button(t("Продолжить"), { kind: "primary", icon: "play", onclick: () => start(from) }),
        button(t("С начала"), { onclick: () => start(0) })));
    stage.append(prompt);
    prompt.querySelector("button").focus();
    function start(seconds) {
      prompt.remove();
      startAt(seconds);
    }
  }

  function startAt(seconds) {
    if (remux) {
      seekTo(seconds, true);
      return;
    }
    video.autoplay = true;
    video.src = source;
    if (seconds) video.currentTime = seconds;
  }

  // часть эфира: следующая включается сама, переключатель — под видео
  function openPart(number, play, start) {
    const part = parts.find((candidate) => candidate.part === number);
    if (!part || part.path === item.path) return;
    modal.close();
    openWatch(Object.assign({}, item, { path: part.path, name: part.name, part: part.part }),
              { autoplay: play, start: start });
  }

  // сколько уже записано; часть кончилась — к новой, эфир кончился — досмотреть эту
  async function pollLive() {
    if (closed || !live) return;
    let answer;
    try {
      answer = await bridge.call("live_status", options.cid, item.path);
    } catch (error) {
      return;                 // запись перенесли в обработку — подождём следующего раза
    }
    if (closed || !live) return;
    if (answer.duration) {
      duration = answer.duration;
      update();
      loadActivity();
    }
    if (liveStalled && duration - position() > LIVE_GAP / 2) {
      liveStalled = false;
      seekTo(position(), true);
    }
    if (answer.growing) return;
    live = false;
    clearInterval(liveTimer);
    liveBadge.hidden = true;
    if (answer.next) {
      // у края — сразу к новой части; перемотали назад — когда досмотрят эту
      if (duration - position() < LIVE_GAP * 3) switchToNext();
      else liveNext = true;
    } else {
      toast(t("Эфир закончился — запись появится в библиотеке, когда обработается"), "ok");
    }
  }

  function switchToNext() {
    modal.close();
    openLive(options.cid);
  }

  video.addEventListener("ended", () => {
    if (liveNext) {
      switchToNext();
      return;
    }
    if (live) {
      // поток дошёл до края записи: подождать, пока она подрастёт
      if (duration - position() > 3) seekTo(position(), true);
      else liveStalled = true;
      return;
    }
    const index = parts.findIndex((part) => part.path === item.path);
    if (index >= 0 && index + 1 < parts.length) openPart(parts[index + 1].part, true);
  });

  // ---------------------------------------------------------------- поиск по чату

  let searchTimer = null, searchGeneration = 0;
  function search() {
    const text = searchInput.value.trim();
    const current = ++searchGeneration;
    if (!text || !chat) {
      found.hidden = true;
      list.hidden = false;
      if (chat) setText(status, chat.live ? t("чат пишется вместе с записью")
                                          : fmt.counted(chat.count, t("сообщение|сообщения|сообщений")));
      return;
    }
    bridge.call("chat_search", chat.id, text).then((messages) => {
      if (current !== searchGeneration || closed) return;
      list.hidden = true;
      found.hidden = false;
      setText(status, t("Найдено: {count}", { count: messages.length }));
      found.replaceChildren(...messages.map((message) => {
        const line = chatLine(message);
        line.classList.add("found");
        line.title = t("Перейти к этому месту");
        line.addEventListener("click", () => {
          // сообщение — на пару секунд раньше, с поправкой на сдвиг чата
          jump(message[0] + shift - 2, true);
          searchInput.value = "";
          search();
        });
        return line;
      }));
      if (!messages.length) found.append(h("div", { class: "watch-chat-status", text: t("Ничего не нашлось.") }));
    }).catch(failed);
  }
  searchInput.addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(search, 300);
  });
  searchInput.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && searchInput.value) {
      event.stopPropagation();
      searchInput.value = "";
      search();
    }
  });

  // проверку записи попросили из сводки — пометки на шкале появятся сразу
  const onChecked = (event) => {
    if (closed) {
      window.removeEventListener("integrity", onChecked);
      return;
    }
    if (event.detail.path === item.path && event.detail.gaps) {
      gaps = event.detail.gaps;
      drawGaps();
    }
  };
  window.addEventListener("integrity", onChecked);

  showChat(prefs.chat);
  setTheatre(prefs.theatre);
  applyFont();
  refreshClip();
  update();
}
