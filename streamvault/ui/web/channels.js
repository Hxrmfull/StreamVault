/*
 * StreamVault, окно программы: каналы — боковая панель, «Эфиры», страница
 * канала, списки записей и клипов, одно видео и запись по ссылке.
 */

import { CHAT_MODES, DOWNLOAD_QUALITIES, KIND_LABELS, PAGE, PERIOD_LABELS, bridge, button, channelName, channelUrl, chatSelect, chatSupported, confirmDialog, displayTitle, ensureProfiles, entryChannelName, failed, fillAvatar, fmt, h, icon, iconButton, isLive, isRecording, isUrl, recordStarting, keepDaysSelect, knownProfile, liveQualityLabel, liveQualitySelect, liveThumb, openModal, platformName, qualitySelect, setSrc, setText, siteInfo, sortedChannels, splitId, state, stateText, stateTone, syncList, t, toast } from "./common.js";
import { twitchdownloaderBlock } from "./settings.js";
import { openAddChannel } from "./search.js";
import { navigate, recBadge, refreshView, render, routeHash, view } from "./app.js";
import { openLive } from "./player.js";

export function refreshSidebar() {
  const list = document.getElementById("channel-list");
  const channels = sortedChannels();
  document.getElementById("sidebar-empty").hidden = channels.length > 0;
  syncList(list, channels, (channel) => channel.id, createSidebarItem, updateSidebarItem);
}

export function createSidebarItem() {
  const li = h("li", { class: "channel-item" });
  li.addEventListener("click", () => navigate(routeHash("channel", li.dataset.key)));
  li._avatar = h("span", { class: "avatar" });
  li._name = h("div", { class: "name" });
  li._sub = h("div", { class: "sub" });
  li._status = h("span", { class: "status" });
  li.append(li._avatar, h("div", { class: "meta" }, li._name, li._sub), li._status);
  return li;
}

export function updateSidebarItem(li, channel) {
  const live = isLive(channel);
  const stream = channel.stream || {};
  li.classList.toggle("offline", !live);
  li.classList.toggle("selected", state.route.name === "channel" && state.route.id === channel.id);
  fillAvatar(li._avatar, channel.id, channel.platform);
  setText(li._name, channelName(channel.id));
  setText(li._sub, live ? (stream.game || stream.title || platformName(channel.platform))
                        : platformName(channel.platform));
  li.title = channelName(channel.id) + " — " + stateText(channel);

  const key = [channel.state, stream.viewers].join("|");
  if (li._status.dataset.key === key) return;
  li._status.dataset.key = key;
  if (live) {
    li._status.replaceChildren(h("span", { class: "dot live" }),
                               stream.viewers !== null && stream.viewers !== undefined ? fmt.count(stream.viewers) : t("в эфире"));
  } else if (stateTone(channel)) {
    li._status.replaceChildren(h("span", { class: "dot error" }));
  } else if (channel.state === "paused") {
    li._status.replaceChildren(t("выкл."));
  } else {
    li._status.replaceChildren();
  }
}

export function renderLive(main) {
  view.processing = h("div");
  view.liveCount = h("span", { class: "count" });
  view.liveGrid = h("div", { class: "grid" });
  view.liveEmpty = h("div", { class: "muted", text: t("Сейчас никто из отслеживаемых каналов не в эфире.") });
  view.offCount = h("span", { class: "count" });
  view.offRows = h("div", { class: "rows" });
  view.offSection = h("div", { class: "section-block" },
    h("h2", { class: "section-title" }, t("Остальные каналы"), view.offCount), view.offRows);
  view.welcome = h("div", { class: "empty" },
    h("strong", { text: t("Добавь первый канал") }),
    t("StreamVault покажет, когда канал в эфире. Записать эфир можно кнопкой, а включив автозапись — записывать каждый."),
    h("div", null, button(t("Добавить канал"), { kind: "primary", icon: "plus", onclick: openAddChannel })));

  main.append(
    view.welcome,
    view.processing,
    h("h2", { class: "section-title" }, t("В эфире"), view.liveCount),
    view.liveEmpty,
    view.liveGrid,
    view.offSection);
  view.refresh = refreshLive;
  refreshLive();
}

export function refreshLive() {
  const channels = sortedChannels();
  const live = channels.filter(isLive);
  const rest = channels.filter((channel) => !isLive(channel));
  view.welcome.hidden = channels.length > 0;
  view.liveEmpty.hidden = live.length > 0 || channels.length === 0;
  setText(view.liveCount, live.length || "");
  setText(view.offCount, rest.length || "");
  view.offSection.hidden = rest.length === 0;
  syncList(view.liveGrid, live, (channel) => channel.id, createLiveCard, updateLiveCard);
  syncList(view.offRows, rest, (channel) => channel.id, createChannelRow, updateChannelRow);

  const processing = state.processing || {};
  view.processing.replaceChildren();
  if (processing.current || processing.pending) {
    const name = processing.current ? processing.current.file.split(/[\\/]/).pop() : "";
    view.processing.append(h("div", { class: "processing-strip" },
      h("span", { class: "spinner small" }),
      processing.current ? h("span", null, t("Обрабатываю запись: "), h("span", { text: name })) : null,
      processing.pending ? h("span", { class: "faint", text: t("в очереди: {count}", { count: processing.pending }) }) : null));
  }
}

export function createLiveCard() {
  const card = h("div", { class: "card" });
  card.addEventListener("click", () => navigate(routeHash("channel", card.dataset.key)));
  card._img = h("img", { alt: "", loading: "lazy" });
  card._img.hidden = true;
  card._topRight = h("div", { class: "corner top-right" });
  card._viewers = h("span", { class: "badge dark" });
  // «Записать» / «Остановить» прямо с карточки — без захода на страницу канала
  card._action = h("button", { type: "button", class: "card-action" });
  card._action.addEventListener("click", (event) => {
    event.stopPropagation();
    quickRecord(card.dataset.key);
  });
  // смотреть идущую запись прямо в окне, с чатом и перемоткой
  card._watch = h("button", { type: "button", class: "card-action", title: t("Смотреть запись с чатом, пока она идёт") },
    icon("play"), t("Смотреть"));
  card._watch.addEventListener("click", (event) => {
    event.stopPropagation();
    openLive(card.dataset.key);
  });
  card._avatar = h("span", { class: "avatar medium" });
  card._title = h("div", { class: "card-title" });
  card._name = h("div", { class: "card-sub" });
  card._game = h("div", { class: "card-sub game" });
  card.append(
    h("div", { class: "thumb" },
      h("div", { class: "placeholder" }, icon("video")), card._img,
      h("div", { class: "corner top-left" }, h("span", { class: "badge live", text: t("В эфире") })),
      card._topRight,
      h("div", { class: "corner bottom-left" }, card._viewers),
      h("div", { class: "corner bottom-right" }, card._watch, card._action)),
    h("div", { class: "card-body" }, card._avatar,
      h("div", { class: "card-text" }, card._title, card._name, card._game)));
  return card;
}

export function updateLiveCard(card, channel) {
  const stream = channel.stream || {};
  setSrc(card._img, liveThumb(stream.thumbnail));
  fillAvatar(card._avatar, channel.id, channel.platform);
  setText(card._title, displayTitle(stream.title) || t("Без названия"));
  card._title.title = stream.title || "";
  setText(card._name, channelName(channel.id) + " · " + platformName(channel.platform));
  setText(card._game, channel.state === "unavailable" ? stateText(channel) : stream.game);
  card._viewers.hidden = stream.viewers === null || stream.viewers === undefined;
  setText(card._viewers, card._viewers.hidden ? "" : fmt.viewers(stream.viewers));

  const key = channel.recording ? "rec:" + channel.recording.file : channel.state;
  if (card._topRight.dataset.key !== key) {
    card._topRight.dataset.key = key;
    card._topRight.replaceChildren(channel.recording ? recBadge(channel)
      : channel.state === "unavailable" ? h("span", { class: "badge warn", text: t("не удаётся записать") })
      : h("span", { class: "badge dark", text: t("без записи") }));
  }
  const recording = isRecording(channel);
  const starting = recordStarting(channel.id, channel);
  card._watch.hidden = !(channel.state === "recording" && channel.recording);
  const busy = state.recordBusy.has(channel.id);
  const actionKey = [recording, starting, busy].join("|");
  if (card._action.dataset.key !== actionKey) {
    card._action.dataset.key = actionKey;
    card._action.disabled = busy;
    card._action.classList.toggle("start", !recording && !starting);
    card._action.title = starting ? t("Запись запускается — нажми, чтобы отменить")
      : recording ? t("Остановить запись; записанное сохранится") : t("Записать идущий эфир сейчас");
    card._action.replaceChildren(starting ? h("span", { class: "spinner small" }) : icon(recording ? "stop" : "record"),
      busy ? t("Подождите…") : starting ? t("Запускаю…") : recording ? t("Остановить") : t("Записать"));
  }
}

// начать или остановить запись канала — с карточки эфира или страницы канала
export function quickRecord(id) {
  const channel = state.channels.get(id);
  // пока запись запускается, кнопка её отменяет
  const recording = isRecording(channel) || recordStarting(id, channel);
  state.recordBusy.add(id);
  refreshView();
  return bridge.call(recording ? "stop_recording" : "record_now", id)
    .then(() => {
      if (recording) {
        state.recordStarting.delete(id);
        toast(t("Запись остановлена, записанное сохранится"), "ok");
      } else {
        // о начале скажет сама кнопка: «Запись начинается…», затем «Остановить запись»
        state.recordStarting.set(id, Date.now());
      }
    })
    .catch(failed)
    .finally(() => {
      state.recordBusy.delete(id);
      refreshView();
    });
}

export function createChannelRow() {
  const row = h("div", { class: "row" });
  row.addEventListener("click", () => navigate(routeHash("channel", row.dataset.key)));
  row._avatar = h("span", { class: "avatar medium" });
  row._title = h("div", { class: "row-title" });
  row._sub = h("div", { class: "row-sub" });
  row._platform = h("span", { class: "badge platform" });
  row.append(row._avatar, h("div", { class: "row-main" }, row._title, row._sub), row._platform);
  return row;
}

export function updateChannelRow(row, channel) {
  fillAvatar(row._avatar, channel.id, channel.platform);
  setText(row._title, channelName(channel.id));
  setText(row._platform, platformName(channel.platform));
  row._sub.className = "row-sub " + stateTone(channel);
  const key = [channel.state, channel.message, channel.last_check, recordStarting(channel.id, channel)].join("|");
  if (row._sub.dataset.key === key) return;
  row._sub.dataset.key = key;
  row._sub.replaceChildren(stateText(channel));
  if (channel.state === "offline" && channel.last_check) {
    const ago = h("span", { class: "faint" });
    ago.dataset.ago = channel.last_check;
    ago.dataset.prefix = t(" · проверено ");
    row._sub.append(ago);
  }
}

export function renderChannel(main, id) {
  const { platform, channel } = splitId(id);
  view.id = id;
  view.platform = platform;
  view.channel = channel;
  view.header = h("div");
  view.livePanel = h("div");
  main.append(view.header, view.livePanel);
  buildChannelHeader();
  view.refresh = () => {
    if (trackKind(state.channels.get(id)) !== view.tracked) buildChannelHeader();
    refreshChannelHeader();
    if (view.browser) view.browser.refreshBadges();
  };
  ensureProfiles([id]);

  const kinds = (siteInfo(platform) || {}).listings;
  if (!kinds || !kinds.length || isUrl(channel)) {
    main.append(h("div", { class: "empty" },
      h("strong", { text: t("Списка записей здесь нет") }),
      t("Открой запись по ссылке через строку сверху.")));
    return;
  }

  const remembered = sessionStorage.getItem("kind:" + id);
  view.kind = kinds.includes(remembered) ? remembered : kinds[0];
  view.period = sessionStorage.getItem("period:" + id) || "7d";
  view.tabs = h("div", { class: "tabs" });
  view.periodSelect = h("select", { title: t("Самые просматриваемые клипы за срок") },
    (state.info.clip_periods || []).map((period) =>
      h("option", { value: period, text: t(PERIOD_LABELS[period]) })));
  view.periodSelect.value = view.period;
  view.periodSelect.addEventListener("change", () => {
    view.period = view.periodSelect.value;
    sessionStorage.setItem("period:" + id, view.period);
    showListing();
  });
  for (const kind of kinds) {
    const label = platform === "youtube" && kind === "vods" ? t("Трансляции") : t(KIND_LABELS[kind] || kind);
    const tab = h("button", { type: "button", text: label });
    tab.dataset.kind = kind;
    tab.addEventListener("click", () => {
      view.kind = kind;
      sessionStorage.setItem("kind:" + id, kind);
      showListing();
    });
    view.tabs.append(tab);
  }
  view.listing = h("div");
  // всё с вкладки: уже скачанное и стоящее в очереди программа пропустит сама
  view.allButton = button(t("Скачать всё"), { icon: "download", title: t("Скачать всё с этой вкладки; " +
    "повторное нажатие докачивает только новое"), onclick: () => downloadAllListing() });
  const missing = Object.values((siteInfo(platform) || {}).unsupported || {});
  main.append(h("div", { class: "toolbar" }, view.tabs, h("span", { class: "spacer" }), view.periodSelect,
                view.allButton));
  if (missing.length) main.append(h("div", { class: "hint", text: missing.join(" · ") }));
  main.append(view.listing);
  showListing();
}

// отслеживается ли канал: none, temporary (пишется по кнопке) или tracked
export function trackKind(channel) {
  return !channel ? "none" : channel.temporary ? "temporary" : "tracked";
}

export function buildChannelHeader() {
  const id = view.id;
  const channel = state.channels.get(id);
  view.tracked = trackKind(channel);
  const tracked = view.tracked === "tracked" ? channel : null;
  view.avatar = h("span", { class: "avatar big" });
  view.title = h("span");
  view.stateLine = h("div", { class: "state-line" });
  const controls = h("div", { class: "channel-controls" });
  view.recordButton = button("", { onclick: () => quickRecord(id) });
  view.recordButton.dataset.mode = "";
  view.watchButton = button(t("Смотреть"), { icon: "play", title: t("Смотреть запись с чатом, пока она идёт"),
                                            onclick: () => openLive(id) });
  view.watchButton.hidden = true;
  view.probed = null;       // разовая проверка канала не из списка

  if (tracked) {
    view.enabled = h("input", { type: "checkbox" });
    view.enabled.addEventListener("change", () => {
      bridge.call("set_enabled", id, view.enabled.checked).catch(failed);
    });
    controls.append(
      view.recordButton, view.watchButton,
      h("label", { class: "switch", title: t("Записывать каждый эфир") }, view.enabled,
        h("span", { class: "track" }), t("Автозапись")),
      button(t("Настройки записи"), { icon: "gear", title: t("Качество, чат, начало эфира, конвертация, " +
                                     "деление на куски и срок хранения записей этого канала"),
                                     onclick: () => openChannelOptions(id) }),
      button(t("Проверить"), { icon: "refresh", title: t("Проверить, не в эфире ли канал, прямо сейчас"),
                            onclick: () => bridge.call("check_now", id).catch(failed) }),
      h("span", { class: "spacer" }),
      button(t("Отслеживается"), { kind: "ghost", icon: "check", title: t("Перестать отслеживать"),
                                   onclick: () => confirmRemoveChannel(id) }));
  } else {
    // «Отслеживать» — на том же месте, где у отслеживаемого канала «Отслеживается»
    controls.append(
      view.recordButton, view.watchButton,
      h("span", { class: "spacer" }),
      button(t("Отслеживать"), {
        kind: "primary", icon: "plus",
        title: t("Добавить в список: StreamVault будет показывать, когда канал в эфире"),
        onclick: () => bridge.call("add_channel", id, null, false, knownProfile(id))
          .then(() => toast(t("Канал отслеживается. Чтобы записывать каждый эфир, включи «Автозапись»."), "ok"))
          .catch(failed),
      }));
    if (!channel) probeUntracked(id);
  }
  const site = channelUrl(id);
  if (site) controls.append(iconButton("external", t("Открыть на сайте"),
                                       () => bridge.call("open_url", site).catch(failed)));

  // кнопки — всегда строкой под именем: и у отслеживаемого канала, и у любого другого
  view.header.replaceChildren(h("div", { class: "channel-header" },
    h("div", { class: "channel-top" },
      view.avatar,
      h("div", { class: "who" },
        h("h1", null, view.title, h("span", { class: "badge platform", text: platformName(view.platform) })),
        view.stateLine)),
    controls));
  refreshChannelHeader();
}

// свои настройки записи канала поверх общих. Списки сразу стоят на значении
// канала, а нет своего — на общем; совпало с общим — у канала своего нет,
// и смена общих настроек его тоже меняет
const SPLIT_MODES = [["off", "не делить"], ["size", "по размеру"], ["duration", "по длительности"],
                     ["count", "на равные части"]];

export function openChannelOptions(id) {
  const channel = state.channels.get(id);
  if (!channel) return;
  const options = channel.options || {};
  const general = state.settings;
  const yesNo = [[true, t("да")], [false, t("нет")]];
  const labelOf = (choices, value) => {
    const found = choices.find(([choice]) => choice === value);
    return found ? found[1] : String(value);
  };
  const choice = (key, choices) => {
    const select = h("select", null,
      choices.map(([value, label]) => h("option", { value: JSON.stringify(value), text: label })));
    const current = key in options ? options[key] : general[key];
    // значение, вписанное в settings.json руками, тоже должно показываться
    if (!choices.some(([value]) => value === current)) {
      select.append(h("option", { value: JSON.stringify(current), text: labelOf(choices, current) }));
    }
    select.value = JSON.stringify(current);
    return select;
  };
  const own = (key, value) => (value === general[key] ? null : value);
  const field = (label, control, hint) => h("div", { class: "field" }, h("label", { text: label }), control,
    hint ? h("div", { class: "hint", text: hint }) : null);
  const chatChoices = CHAT_MODES.map(([mode, label]) => [mode, t(label)]);
  const splitChoices = SPLIT_MODES.map(([mode, label]) => [mode, t(label)]);
  const fields = {
    record_from_start: choice("record_from_start", yesNo),
    join_beginning: choice("join_beginning", yesNo),
    live_chat: choice("live_chat", chatChoices),
    convert_recordings: choice("convert_recordings", yesNo),
    mp4_faststart: choice("mp4_faststart", yesNo),
    split_mode: choice("split_mode", splitChoices),
  };
  // у каждого способа деления своё число; стоит общее, пока у канала нет своего
  const number = (key, step) => {
    const input = h("input", { type: "number", step: step || "1", placeholder: String(general[key]) });
    input.value = String(key in options ? options[key] : general[key]);
    return input;
  };
  const numbers = { split_size_gb: number("split_size_gb", "0.1"), split_minutes: number("split_minutes"),
                    split_count: number("split_count") };
  const quality = liveQualitySelect(channel.quality || general.quality);
  const keep = keepDaysSelect(channel.keep_days === undefined || channel.keep_days === null
                              ? general.keep_days || 0 : channel.keep_days);
  const numberFields = {
    size: field(t("Кусок не больше, ГБ"), numbers.split_size_gb),
    duration: field(t("Кусок длиной, минут"), numbers.split_minutes),
    count: field(t("На сколько частей"), numbers.split_count),
  };
  const showNumbers = () => {
    const mode = JSON.parse(fields.split_mode.value);
    for (const [name, element] of Object.entries(numberFields)) element.hidden = name !== mode;
  };
  fields.split_mode.addEventListener("change", showNumbers);
  showNumbers();
  const modal = openModal();
  const save = button(t("Сохранить"), { kind: "primary", onclick: () => {
    const changes = {};
    for (const [key, select] of Object.entries(fields)) changes[key] = own(key, JSON.parse(select.value));
    for (const [key, input] of Object.entries(numbers)) {
      const value = input.value.trim().replace(",", ".");
      // непонятное число не превращается молча в общее — его проверит программа
      changes[key] = value === "" ? null : Number.isNaN(Number(value)) ? value : own(key, Number(value));
    }
    save.disabled = true;
    const days = Number(keep.value) === (general.keep_days || 0) ? null : Number(keep.value);
    const qualityOwn = quality.value === general.quality ? null : quality.value;
    const calls = [bridge.call("set_channel_options", id, changes)];
    if (qualityOwn !== (channel.quality || null)) calls.push(bridge.call("set_quality", id, qualityOwn));
    if (days !== (channel.keep_days === undefined ? null : channel.keep_days)) calls.push(bridge.call("set_keep_days", id, days));
    Promise.all(calls)
      .then(() => {
        modal.close();
        toast(t("Настройки записи канала сохранены"), "ok");
      })
      .catch((error) => {
        save.disabled = false;
        failed(error);
      });
  } });
  modal.body.append(
    h("h2", { text: t("Настройки записи канала") }),
    h("div", { class: "muted field-gap", text: t("Сразу выбрано то, что стоит в общих настройках; изменённое здесь действует только для этого канала. Идущая запись подхватит изменения со следующей части, конвертация — со следующей записи.") }),
    field(t("Качество записи"), quality),
    field(t("Чат прямых эфиров при записи"), fields.live_chat),
    field(t("Записывать с начала эфира, если запись началась позже"), fields.record_from_start),
    field(t("Склеивать начало эфира с записью"), fields.join_beginning),
    field(t("Конвертировать записи в mp4"), fields.convert_recordings),
    field(t("Готовить mp4 к просмотру по сети"), fields.mp4_faststart),
    field(t("Делить запись на куски"), fields.split_mode),
    numberFields.size, numberFields.duration, numberFields.count,
    field(t("Хранить записи"), keep),
    h("div", { class: "actions" }, button(t("Отмена"), { onclick: () => modal.close() }), save));
}

// канал не из списка: в эфире ли он — чтобы «Начать запись» не нажимали вслепую
export function probeUntracked(id) {
  view.probed = { id: id, platform: view.platform, state: "starting", stream: null, probe: null };
  bridge.call("check_channel", id).then((answer) => {
    if (view.id !== id || !view.probed) return;
    Object.assign(view.probed, answer);
    refreshChannelHeader();
  }).catch(() => {
    if (view.id === id && view.probed) view.probed.state = "unknown";
    refreshChannelHeader();
  });
}

export function refreshChannelHeader() {
  const id = view.id;
  // канал не из списка показывается по разовой проверке
  const channel = state.channels.get(id) || view.probed;
  fillAvatar(view.avatar, id, view.platform);
  setText(view.title, channelName(id));
  const live = channel && isLive(channel);
  view.avatar.classList.toggle("live-ring", Boolean(live));

  if (channel && view.tracked === "tracked") {
    view.enabled.checked = channel.enabled !== false;
  }
  refreshRecordButton();
  const key = channel ? [channel.state, channel.message, channel.last_check,
                         (channel.stream || {}).viewers, channel.probe,
                         recordStarting(channel.id, channel)].join("|") : "untracked";
  if (view.stateLine.dataset.key !== key) {
    view.stateLine.dataset.key = key;
    view.stateLine.replaceChildren();
    view.stateLine.title = "";
    if (!channel || channel.state === "unknown") {
      view.stateLine.append(t("Не отслеживается — можно смотреть и скачивать записи"));
    } else {
      if (channel.temporary || channel === view.probed) {
        view.stateLine.append(h("span", { class: "faint", text: t("не отслеживается ·") }));
      }
      const tone = stateTone(channel);
      view.stateLine.append(h("span", { class: "dot" + (live ? " live" : tone ? " " + tone : "") }),
                            h("span", { text: stateText(channel) }));
      if (live && channel.stream && channel.stream.viewers !== null && channel.stream.viewers !== undefined) {
        view.stateLine.append(h("span", { class: "faint", text: "· " + fmt.viewers(channel.stream.viewers) }));
      }
      if (channel.state === "offline" && channel.last_check) {
        const ago = h("span", { class: "faint" });
        ago.dataset.ago = channel.last_check;
        ago.dataset.prefix = t("· проверено ");
        view.stateLine.append(ago);
      }
      // как проверяется канал — подробность для любопытных, в подсказке
      if (channel.probe) {
        view.stateLine.title = t("проверка через {probe} раз в {seconds} с", {
          probe: channel.probe === "helix" ? t("API Twitch") : "streamlink",
          seconds: channel.refresh || "?" });
      }
    }
  }

  const panelKey = live ? [channel.state, (channel.recording || {}).file,
                           (channel.stream || {}).title, (channel.stream || {}).thumbnail,
                           JSON.stringify(channel.beginning || null)].join("|") : "";
  if (view.livePanel.dataset.key === panelKey) return;
  view.livePanel.dataset.key = panelKey;
  view.livePanel.replaceChildren();
  if (!live) return;
  const stream = channel.stream || {};
  const img = h("img", { alt: "" });
  setSrc(img, liveThumb(stream.thumbnail));
  // состояние уже в шапке — здесь только эфир и, если пишется, запись
  view.livePanel.append(h("div", { class: "live-panel" },
    h("div", { class: "thumb" }, h("div", { class: "placeholder" }, icon("video")), img,
      h("div", { class: "corner top-left" }, h("span", { class: "badge live", text: t("В эфире") }))),
    h("div", { class: "live-info" },
      h("div", { class: "live-title", text: displayTitle(stream.title) || t("Без названия") }),
      stream.game ? h("div", { class: "muted", text: stream.game }) : null,
      channel.state === "unavailable" ? h("div", { class: "warn-text", text: stateText(channel) }) : null,
      channel.recording ? h("div", null, recBadge(channel)) : null,
      channel.recording ? h("div", { class: "faint", text:
        t("Качество: {quality}", { quality: liveQualityLabel(channel.recording.quality || channel.quality) })
        + (channel.recording.part > 1 ? " · " + t("часть {part}", { part: channel.recording.part }) : "") }) : null,
      beginningLine(channel.beginning),
      channel.recording ? h("div", { class: "toolbar" },
        button(t("Открыть папку записей"), { icon: "folder",
          onclick: () => bridge.call("open_folder", "recordings").catch(failed) })) : null)));
}

// запись началась позже эфира: что с его началом (live/beginning.py)
function beginningLine(beginning) {
  if (!beginning) return null;
  const length = beginning.length ? fmt.duration(beginning.length) : "";
  const text = {
    searching: t("Начало эфира ({length}): ищу его запись на площадке", { length: length }),
    waiting: t("Начало эфира ({length}) скачается, когда площадка выложит запись эфира", { length: length }),
    queued: t("Начало эфира ({length}) скачивается — «Загрузки»", { length: length }),
    unavailable: t("Начало эфира не взять: {reason}", { reason: beginning.reason || t("записи эфира на площадке нет") }),
  }[beginning.state];
  return text ? h("div", { class: beginning.state === "unavailable" ? "warn-text" : "faint", text: text }) : null;
}

// «Начать запись»: яркая, когда канал в эфире; во время записи — «Остановить запись»
export function refreshRecordButton() {
  const channel = state.channels.get(view.id) || view.probed;
  const recording = isRecording(channel);
  const starting = recordStarting(view.id, channel);
  view.watchButton.hidden = !(channel && channel.state === "recording" && channel.recording);
  const busy = state.recordBusy.has(view.id);
  const live = Boolean(channel) && isLive(channel);
  const mode = [busy, recording, starting, live].join("|");
  if (view.recordButton.dataset.mode === mode) return;
  view.recordButton.dataset.mode = mode;
  view.recordButton.disabled = busy;
  view.recordButton.className = "btn" + (starting ? " stop-record starting" : recording ? " stop-record"
                                         : live ? " record-live" : "");
  view.recordButton.title = starting ? t("Запись запускается — нажми, чтобы отменить")
    : recording ? t("Остановить запись; записанное сохранится")
    : live ? t("Записать идущий эфир сейчас") : t("Проверить, не в эфире ли канал, и записать");
  view.recordButton.replaceChildren(starting ? h("span", { class: "spinner small" }) : icon(recording ? "stop" : "record"),
    busy ? (recording || starting ? t("Останавливаю…") : t("Проверяю эфир…"))
         : starting ? t("Запись начинается…") : recording ? t("Остановить запись") : t("Начать запись"));
}

export function downloadAllListing() {
  const { platform, channel, kind } = view;
  const clips = ((siteInfo(platform) || {}).periodic || []).includes(kind);
  const period = clips ? view.period : "all";
  const tab = (view.tabs.querySelector(".active") || {}).textContent || kind;
  confirmDialog(t("Скачать всё с вкладки «{tab}»?", { tab: tab }),
    t("В загрузки встанет всё, чего ещё нет: уже скачанное и стоящее в очереди пропускается. " +
      "Качество и чат — как в настройках."),
    t("Скачать всё"), () => {
      const buttonEl = view.allButton;
      buttonEl.disabled = true;
      buttonEl.replaceChildren(icon("download"), t("Собираю список…"));
      bridge.call("download_all", platform, channel, kind, period)
        .then((result) => toast(result.added
          ? t("Добавлено в загрузки: {count}, уже было: {known}", { count: result.added, known: result.known })
          : t("Нового нет: всё с этой вкладки уже скачано или в очереди"), "ok", result.added ? "#downloads" : null))
        .catch(failed)
        .finally(() => {
          buttonEl.disabled = false;
          buttonEl.replaceChildren(icon("download"), t("Скачать всё"));
        });
    }, "primary");
}

export function showListing() {
  for (const tab of view.tabs.children) tab.classList.toggle("active", tab.dataset.kind === view.kind);
  const clips = ((siteInfo(view.platform) || {}).periodic || []).includes(view.kind);
  view.periodSelect.hidden = !clips;
  const period = clips ? view.period : "all";
  const key = [view.id, view.kind, period].join("|");
  const { platform, channel, kind } = view;
  view.browser = createVideoBrowser(key, (start) =>
    bridge.call("list_channel", platform, channel, kind, start, PAGE, period));
  view.listing.replaceChildren(view.browser.el);
}

export function confirmRemoveChannel(id) {
  confirmDialog(t("Перестать отслеживать {name}?", { name: channelName(id) }),
    t("Идущая запись остановится. Уже записанное останется на диске."),
    t("Перестать отслеживать"),
    () => bridge.call("remove_channel", id).then(() => toast(t("Канал убран из списка"), "ok")).catch(failed));
}

/*
 * Сетка видео с подгрузкой страницами и выбором для скачивания.
 * Загруженные страницы помнятся: вернувшись на вкладку, не ждёшь заново.
 */
export function createVideoBrowser(key, load) {
  let listing = state.listings.get(key);
  if (!listing) {
    listing = { entries: [], start: 1, done: false, error: "" };
    state.listings.set(key, listing);
  }
  const selected = new Map();
  const grid = h("div", { class: "grid" });
  const status = h("div");
  const more = button(t("Показать ещё"), { onclick: () => loadMore() });
  const moreBox = h("div", { class: "more" }, more);
  const count = h("span", { class: "muted" });
  const quality = qualitySelect(DOWNLOAD_QUALITIES);
  const chatMode = chatSelect();
  const chatLabel = h("span", { class: "muted", text: t("Чат") });
  const bar = h("div", { class: "selection-bar" }, count, h("span", { class: "spacer" }),
    h("span", { class: "muted", text: t("Качество") }), quality, chatLabel, chatMode,
    button(t("Снять выбор"), { kind: "ghost", onclick: () => { selected.clear(); refresh(); } }),
    button(t("Скачать"), { kind: "primary", icon: "download", onclick: () => downloadSelected() }));
  const el = h("div", null, grid, status, moreBox, bar);
  let loading = false;

  function refresh() {
    syncList(grid, listing.entries, (entry) => entry.url || entry.id, createVideoCard, updateCard);
    grid.classList.toggle("selecting", selected.size > 0);
    bar.hidden = selected.size === 0;
    chatLabel.hidden = chatMode.hidden = ![...selected.values()].some(chatSupported);
    setText(count, t("Выбрано: {count}", { count: selected.size }));
    moreBox.hidden = listing.done || Boolean(listing.error) || listing.entries.length === 0;
    more.disabled = loading;
    status.replaceChildren();
    if (loading) status.append(h("div", { class: "spinner" }));
    else if (listing.error) {
      status.append(h("div", { class: "empty" }, h("strong", { text: t("Не удалось получить список") }),
        h("span", { text: listing.error }),
        h("div", null, button(t("Повторить"), { onclick: () => { listing.error = ""; loadMore(); } }))));
    } else if (listing.done && listing.entries.length === 0) {
      // Twitch бывает отвечает пустым списком там, где записи есть
      status.append(h("div", { class: "empty" }, h("span", { text: t("Здесь пока пусто.") }),
        h("div", null, button(t("Обновить"), { icon: "refresh", onclick: () => {
          listing.done = false;
          listing.start = 1;
          loadMore();
        } }))));
    }
  }

  function updateCard(card, entry) {
    updateVideoCard(card, entry);
    card.classList.toggle("selected", selected.has(entry.url));
    card._toggle = () => {
      if (selected.has(entry.url)) selected.delete(entry.url); else selected.set(entry.url, entry);
      refresh();
    };
  }

  async function loadMore() {
    if (loading) return;
    loading = true;
    refresh();
    try {
      const result = await load(listing.start);
      const entries = result.entries || [];
      const known = new Set(listing.entries.map((entry) => entry.url));
      listing.entries.push(...entries.filter((entry) => !known.has(entry.url)));
      listing.start += PAGE;
      listing.done = entries.length < PAGE;
      if (result.title) listing.title = result.title;
    } catch (error) {
      listing.error = error.message;
    } finally {
      loading = false;
      refresh();
    }
  }

  async function downloadSelected() {
    const entries = [...selected.values()];
    let added = 0;
    for (const entry of entries) {
      try {
        await bridge.call("add_download", entry.url, quality.value, null, entryMeta(entry), false,
                          chatSupported(entry) ? chatMode.value : null);
        added++;
      } catch (error) {
        failed(error);
      }
    }
    selected.clear();
    refresh();
    if (added) toast(t("Добавлено в загрузки: {count}", { count: added }), "ok", "#downloads");
  }

  refresh();
  if (!listing.entries.length && !listing.done && !listing.error) loadMore();
  return { el: el, refreshBadges: refresh };
}

export function entryMeta(entry) {
  return {
    id: entry.id, title: entry.title, channel: entry.channel, folder: entry.folder, platform: entry.platform,
    kind: entry.kind, duration: entry.duration, thumbnail: entry.thumbnail, timestamp: entry.timestamp,
  };
}

export function jobForUrl(url) {
  let found = null;
  for (const job of state.jobs.values()) {
    if (job.url === url && (!found || job.created > found.created)) found = job;
  }
  return found;
}

export function jobBadge(job) {
  if (!job) return null;
  if (job.state === "done") return h("span", { class: "badge done", text: t("скачано") });
  if (job.state === "failed") return h("span", { class: "badge", text: t("ошибка") });
  if (job.state === "cancelled") return null;
  if (job.state === "paused") return h("span", { class: "badge queued", text: t("на паузе") });
  const percent = job.progress && job.progress.percent;
  return h("span", { class: "badge queued",
                     text: percent !== null && percent !== undefined ? Math.floor(percent) + "%" : t("в загрузках") });
}

export function createVideoCard() {
  const card = h("div", { class: "card" });
  card._img = h("img", { alt: "", loading: "lazy" });
  card._img.hidden = true;
  card._duration = h("span", { class: "badge dark" });
  card._job = h("div", { class: "corner top-right" });
  card._title = h("div", { class: "card-title" });
  card._sub = h("div", { class: "card-sub" });
  const box = h("span", { class: "select-box", title: t("Выбрать для скачивания") });
  box.addEventListener("click", (event) => { event.stopPropagation(); card._toggle(); });
  card.addEventListener("click", () => openVideoModal(card._entry));
  card.append(
    h("div", { class: "thumb" }, h("div", { class: "placeholder" }, icon("video")), card._img,
      box, card._job, h("div", { class: "corner bottom-left" }, card._duration)),
    h("div", { class: "card-body" }, h("div", { class: "card-text" }, card._title, card._sub)));
  return card;
}

export function updateVideoCard(card, entry) {
  card._entry = entry;
  setSrc(card._img, entry.thumbnail);
  card._duration.hidden = !entry.duration;
  setText(card._duration, fmt.duration(entry.duration));
  setText(card._title, displayTitle(entry.title) || t("Без названия"));
  card._title.title = entry.title || "";
  setText(card._sub, [entry.timestamp ? fmt.ago(entry.timestamp) : "",
                      entry.view_count !== null && entry.view_count !== undefined ? fmt.views(entry.view_count) : ""]
                     .filter(Boolean).join(" · "));
  const job = jobForUrl(entry.url);
  const key = job ? job.state + (job.progress ? Math.floor(job.progress.percent || 0) : "") : "";
  if (card._job.dataset.key !== key) {
    card._job.dataset.key = key;
    card._job.replaceChildren(jobBadge(job) || "");
  }
}

export function openVideoModal(entry) {
  const modal = openModal({ wide: true });
  modal.body.append(videoDetail(entry, () => modal.close()));
}

/*
 * Карточка видео с формой скачивания. Варианты качества спрашиваются
 * у yt-dlp при открытии: в списке записей канала их нет.
 */
export function videoDetail(entry, onDone) {
  const img = h("img", { alt: "" });
  setSrc(img, entry.thumbnail);
  const qualities = h("select");
  for (const option of DOWNLOAD_QUALITIES) qualities.append(h("option", { value: option.quality, text: t(option.label) }));
  qualities.value = state.settings.download_quality || "best";
  const loadingNote = h("span", { class: "faint" }, h("span", { class: "spinner small" }), t(" узнаю варианты качества…"));

  const useSection = h("input", { type: "checkbox" });
  const from = h("input", { type: "text", class: "time", placeholder: "0:00:00" });
  const to = h("input", { type: "text", class: "time", placeholder: fmt.duration(entry.duration) || "1:00:00" });
  const sectionRow = h("div", { class: "form-row" }, h("label", null, t("с")), from, h("label", null, t("по")), to);
  sectionRow.hidden = true;
  useSection.addEventListener("change", () => { sectionRow.hidden = !useSection.checked; });
  const error = h("div", { class: "error-text" });
  const jobLine = h("div");
  const submit = button(t("Скачать"), { kind: "primary", icon: "download", type: "submit" });
  const withChat = chatSupported(entry);
  const chatMode = chatSelect();
  const chatHint = twitchdownloaderBlock(true, chatMode);

  const form = h("form", { class: "download-form" },
    h("div", { class: "form-row" }, h("label", null, t("Качество")), qualities, loadingNote),
    withChat ? h("div", { class: "form-row" }, h("label", null, t("Чат")), chatMode) : null,
    withChat ? chatHint : null,
    h("label", { class: "switch" }, useSection, h("span", { class: "track" }), t("Только отрезок")),
    sectionRow, error,
    h("div", { class: "toolbar" }, submit,
      entry.url ? button(t("Открыть на сайте"), { icon: "external",
                                                onclick: () => bridge.call("open_url", entry.url).catch(failed) }) : null),
    jobLine);

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setText(error, "");
    let section = null;
    if (useSection.checked) {
      const end = to.value.trim() || (entry.duration ? fmt.duration(entry.duration) : "");
      if (!end) { setText(error, t("Укажи, где отрезок заканчивается")); return; }
      section = [from.value.trim() || "0", end];
    }
    submit.disabled = true;
    try {
      await bridge.call("add_download", entry.url, qualities.value, section, entryMeta(entry), false,
                        withChat ? chatMode.value : null);
      toast(t("Добавлено в загрузки"), "ok", "#downloads");
      if (onDone) onDone();
    } catch (problem) {
      setText(error, problem.message);
    } finally {
      submit.disabled = false;
    }
  });

  function showJob() {
    const job = jobForUrl(entry.url);
    jobLine.replaceChildren();
    if (job && job.state === "done" && job.file) {
      const chatFiles = job.chat_files || {};
      jobLine.append(h("div", { class: "toolbar" }, h("span", { class: "badge done", text: t("уже скачано") }),
        button(t("Открыть"), { icon: "play", onclick: () => bridge.call("open_file", job.file).catch(failed) }),
        button(t("Показать в папке"), { icon: "folder", onclick: () => bridge.call("show_in_folder", job.file).catch(failed) }),
        chatFiles.html ? button(t("Открыть чат"), { icon: "chat", onclick: () => bridge.call("open_file", chatFiles.html).catch(failed) })
          : withChat && !job.chat_only ? button(t("Скачать чат"), { icon: "chat", onclick: () =>
              bridge.call("download_chat", job.id, chatMode.value === "none" ? null : chatMode.value).catch(failed) }) : null));
    } else if (job && job.state !== "cancelled") {
      jobLine.append(h("div", { class: "toolbar" }, jobBadge(job),
        h("a", { href: "#downloads", text: t("в загрузках") })));
    }
  }
  showJob();

  const meta = h("div", { class: "muted" });
  const info = h("div", { class: "detail-info" },
    h("h2", { text: displayTitle(entry.title) || t("Без названия") }),
    meta, form);
  function showMeta() {
    setText(meta, [entryChannelName(entry.platform, entry.channel), platformName(entry.platform),
                   entry.timestamp ? fmt.date(entry.timestamp) : "",
                   entry.duration ? fmt.duration(entry.duration) : "",
                   entry.view_count ? fmt.views(entry.view_count) : ""].filter(Boolean).join(" · "));
  }
  showMeta();

  // точные варианты качества и длительность — от yt-dlp; страница
  // "по ссылке" уже спросила его, и второй раз ждать незачем
  const details = entry.qualities ? Promise.resolve(entry) : bridge.call("inspect", entry.url);
  details.then((full) => {
    if (full.type !== "video") { loadingNote.replaceChildren(t("это список, а не одно видео")); return; }
    const previous = qualities.value;
    qualities.replaceChildren();
    for (const option of full.qualities) qualities.append(h("option", { value: option.quality, text: t(option.label) }));
    qualities.value = [...qualities.options].some((o) => o.value === previous) ? previous : "best";
    loadingNote.replaceChildren();
    for (const key of ["duration", "timestamp", "channel", "thumbnail", "title", "id", "kind", "platform"]) {
      if (!entry[key] && full[key]) entry[key] = full[key];
    }
    if (!img.getAttribute("src")) setSrc(img, entry.thumbnail);
    to.placeholder = fmt.duration(entry.duration) || to.placeholder;
    showMeta();
  }).catch((problem) => {
    loadingNote.replaceChildren(h("span", { class: "row-sub error", text: problem.message }));
  });

  return h("div", { class: "video-detail" },
    h("div", { class: "thumb" }, h("div", { class: "placeholder" }, icon("video")), img), info);
}

export async function renderLink(main, url) {
  const shown = view;
  main.append(h("h1", { class: "page-title", text: t("По ссылке") }),
              h("div", { class: "muted", text: url }),
              h("div", { class: "spinner" }));
  let result;
  try {
    result = await bridge.call("inspect", url, PAGE, 1);
  } catch (error) {
    if (view !== shown) return;
    main.lastChild.replaceWith(h("div", { class: "empty" },
      h("strong", { text: t("Не получилось открыть ссылку") }), h("span", { text: error.message }),
      h("div", null, button(t("Повторить"), { onclick: () => render() }))));
    return;
  }
  if (view !== shown) return;
  main.lastChild.remove();
  if (result.type === "video") {
    main.append(videoDetail(result, null));
    view.refresh = null;
    return;
  }
  const key = "link|" + url;
  const listing = state.listings.get(key);
  if (!listing) {
    state.listings.set(key, { entries: result.entries, start: 1 + PAGE,
                              done: result.entries.length < PAGE, error: "" });
  }
  main.append(h("h2", { class: "section-title", text: result.title || t("Список видео") }));
  view.browser = createVideoBrowser(key, (start) => bridge.call("inspect", url, PAGE, start));
  view.refresh = () => view.browser.refreshBadges();
  main.append(view.browser.el);
}
