/*
 * StreamVault, окно программы: сводка эфира и поиск по чатам всех записей.
 *
 * Сводка — по всем частям эфира: длительность, размер, чат (сообщений,
 * зрителей, самые активные, частые смайлы), горячие моменты — всплески
 * чата, к ним можно перейти, — и пропуски в записи (проверка — по кнопке).
 * Поиск по чатам ищет слово или ник во всех .chat.json библиотеки сразу
 * и открывает запись на найденном месте.
 */

import { bridge, button, channelName, displayTitle, failed, fmt, h, openModal, platformName, setText, t, tp }
  from "./common.js";
import { openWatch } from "./player.js";

const PEAK_LEAD = 15;         // всплеск чата — реакция: смотреть с 15 секунд до него
const SEARCH_AGAIN = 300;     // не все чаты ещё в базе — спросить снова через столько мс

// пропуски части: текст для человека
function gapsText(gaps) {
  if (gaps === null || gaps === undefined) return t("не проверялась");
  if (!gaps.length) return t("пропусков нет");
  const lost = gaps.reduce((sum, gap) => sum + (gap.length || 0), 0);
  return gaps.length + " " + tp(gaps.length, t("пропуск|пропуска|пропусков"))
    + (lost ? " · " + t("пропало {length}", { length: fmt.duration(lost) }) : "");
}

// item — запись (или эфир из частей); options.jump(путь, секунды) — перейти к месту:
// из проигрывателя — в нём же, из библиотеки — открыть проигрыватель
export function openSummary(item, options) {
  options = options || {};
  const modal = openModal({ wide: true, kind: "summary" });
  const body = h("div", { class: "summary" }, h("div", { class: "spinner" }));
  modal.body.append(
    h("h2", { text: t("Сводка эфира") }),
    h("div", { class: "muted", text: [displayTitle(item.title) || item.title, channelName(item.channel_id, item.channel),
                                      platformName(item.platform), fmt.date(item.timestamp)].filter(Boolean).join(" · ") }),
    body);
  const jump = (path, seconds) => {
    modal.close();
    if (options.jump) {
      options.jump(path, seconds);
      return;
    }
    const part = (item.parts || [item]).find((candidate) => candidate.path === path) || Object.assign({}, item, { path: path });
    openWatch(part, { start: seconds });
  };
  let summary = null;
  const load = () => bridge.call("broadcast_summary", (item.parts || [item])[0].path)
    .then((answer) => {
      summary = answer;
      draw();
    })
    .catch((error) => {
      body.replaceChildren(h("div", { class: "warn-text", text: error.message || String(error) }));
    });
  // проверка части закончилась — сводка обновляется сама
  const onChecked = (event) => {
    if (!body.isConnected) {
      window.removeEventListener("integrity", onChecked);
      return;
    }
    if (summary && summary.parts.some((part) => part.path === event.detail.path)) load();
  };
  window.addEventListener("integrity", onChecked);

  function draw() {
    const parts = summary.parts;
    const duration = parts.every((part) => part.duration) ? parts.reduce((sum, part) => sum + part.duration, 0) : null;
    const stat = (label, value) => h("div", { class: "usage-stat" }, h("div", { class: "muted", text: label }),
                                     h("strong", { text: value }));
    const place = (index, seconds) => (parts.length > 1 ? t("часть {part}", { part: index + 1 }) + " · " : "")
      + fmt.duration(seconds);
    const sections = [h("div", { class: "usage-stats" },
      duration ? stat(t("Длительность"), fmt.duration(duration)) : null,
      stat(t("Размер"), fmt.size(parts.reduce((sum, part) => sum + part.size, 0))),
      parts.length > 1 ? stat(t("Частей"), String(parts.length)) : null,
      stat(t("Сообщений в чате"), fmt.count(summary.messages)),
      summary.messages ? stat(t("Писали в чат"), fmt.counted(summary.chatters, t("зритель|зрителя|зрителей"))) : null,
      summary.messages && duration ? stat(t("Сообщений в минуту"),
                                          String(Math.round(summary.messages / Math.max(1, duration / 60)))) : null)];

    if (summary.moments.length) {
      sections.push(h("h3", { class: "subhead", text: t("Горячие моменты") }),
        h("div", { class: "hint field-gap", text: t("Где чат писал больше всего — щелчок откроет запись чуть раньше всплеска.") }),
        h("div", { class: "summary-list" }, summary.moments.map((moment) => {
          const row = h("button", { type: "button", class: "summary-row" },
            h("span", { class: "summary-time", text: place(moment.part, moment.at) }),
            h("span", { text: fmt.counted(moment.count, t("сообщение|сообщения|сообщений"))
                              + " " + t("за {length}", { length: fmt.duration(moment.length) }) }));
          row.addEventListener("click", () => jump(parts[moment.part].path, Math.max(0, moment.at - PEAK_LEAD)));
          return row;
        })));
    }
    const top = (title, rows) => h("div", null, h("h3", { class: "subhead", text: title }),
      h("div", { class: "summary-top" }, rows.map(([name, count]) => h("div", { class: "summary-top-row" },
        h("span", { class: "summary-name", text: name }), h("span", { class: "muted", text: fmt.count(count) })))));
    if (summary.top_chatters.length || summary.top_emotes.length) {
      sections.push(h("div", { class: "two" },
        summary.top_chatters.length ? top(t("Самые активные в чате"), summary.top_chatters) : h("div"),
        summary.top_emotes.length ? top(t("Частые смайлы"), summary.top_emotes) : h("div")));
    }
    if (!summary.messages) sections.push(h("div", { class: "muted field-gap", text: t("Чата у этой записи нет.") }));

    // пропуски: у записей эфиров ищутся сами при конвертации, у прочего — по кнопке
    sections.push(h("h3", { class: "subhead", text: t("Пропуски в записи") }),
      h("div", { class: "hint field-gap", text: t("Куски эфира, которых нет в файле: сеть или диск не успевали, реклама Twitch. " +
        "На шкале проигрывателя они помечены.") }),
      h("div", { class: "summary-list" }, parts.map((part, index) => {
        const row = h("div", { class: "summary-gaps" },
          h("span", { class: "summary-time", text: parts.length > 1 ? t("часть {part}", { part: index + 1 }) : t("Запись") }),
          h("span", { text: gapsText(part.gaps) }));
        if (part.gaps === null || part.gaps === undefined) {
          const check = button(t("Проверить"), { kind: "ghost", onclick: () => {
            check.disabled = true;
            setText(check, t("проверяю…"));
            bridge.call("check_recording", part.path)
              .then((answer) => { if (answer.gaps) load(); })
              .catch((error) => {
                check.disabled = false;
                setText(check, t("Проверить"));
                failed(error);
              });
          } });
          row.append(check);
        }
        for (const gap of part.gaps || []) {
          const link = h("button", { type: "button", class: "summary-gap",
            text: fmt.duration(gap.at) + (gap.length ? " · " + fmt.duration(gap.length) : "") });
          link.title = t("Перейти к этому месту");
          link.addEventListener("click", () => jump(part.path, Math.max(0, gap.at - 5)));
          row.append(link);
        }
        return row;
      })));
    body.replaceChildren(...sections);
  }
  load();
}

// поиск по чатам всех записей; items — записи библиотеки: по ним подписи и что открыть
export function openChatSearch(items) {
  const byPath = new Map(items.map((item) => [item.path, item]));
  const modal = openModal({ wide: true, kind: "chat-search" });
  const input = h("input", { type: "search", class: "chat-search-input", spellcheck: "false",
                             placeholder: t("Слово, фраза или смайл") });
  const by = h("select", { title: t("Где искать") },
    h("option", { value: "text", text: t("в сообщениях") }), h("option", { value: "name", text: t("по нику") }));
  const status = h("div", { class: "muted field-gap" });
  const results = h("div", { class: "chat-search-results" });
  modal.body.append(h("h2", { text: t("Поиск по чатам") }),
    h("div", { class: "hint field-gap", text: t("Ищет во всех чатах библиотеки сразу. Первый поиск дольше: чаты переписываются в базу программы.") }),
    h("div", { class: "toolbar" }, input, by), status, results);
  input.focus();
  let generation = 0, timer = null;

  function search() {
    const text = input.value.trim();
    const current = ++generation;
    clearTimeout(timer);
    if (!text) {
      setText(status, "");
      results.replaceChildren();
      return;
    }
    setText(status, t("ищу…"));
    bridge.call("search_chats", text, by.value).then((answer) => {
      if (current !== generation || !results.isConnected) return;
      show(answer);
      // не все чаты ещё в базе — досматриваем, пока не кончатся
      if (answer.pending) timer = setTimeout(() => { if (current === generation) search(); }, SEARCH_AGAIN);
    }).catch((error) => {
      if (current === generation) setText(status, error.message || String(error));
    });
  }

  function show(answer) {
    const found = answer.results.filter((result) => byPath.has(result.video))
      .sort((a, b) => (byPath.get(b.video).timestamp || 0) - (byPath.get(a.video).timestamp || 0));
    const total = found.reduce((sum, result) => sum + result.count, 0);
    setText(status, (found.length
      ? t("Найдено {count} в записях: {files}", { count: fmt.counted(total, t("сообщение|сообщения|сообщений")),
                                                 files: found.length })
      : t("Ничего не нашлось."))
      + (answer.pending ? " " + t("Ещё не просмотрено чатов: {count}", { count: answer.pending }) : ""));
    results.replaceChildren(...found.map((result) => {
      const item = byPath.get(result.video);
      return h("div", { class: "chat-search-group" },
        h("div", { class: "chat-search-head" },
          h("strong", { text: displayTitle(item.title) || item.title }),
          h("span", { class: "muted", text: [channelName(item.channel_id, item.channel), platformName(item.platform),
                                              fmt.date(item.timestamp), item.part ? t("часть {part}", { part: item.part }) : ""]
                                             .filter(Boolean).join(" · ") }),
          result.count > result.messages.length
            ? h("span", { class: "faint", text: t("ещё {count}", { count: result.count - result.messages.length }) }) : null),
        result.messages.map(([offset, name, text]) => {
          const line = h("button", { type: "button", class: "chat-line found", title: t("Перейти к этому месту") },
            h("span", { class: "chat-time", text: fmt.duration(offset) }),
            h("span", { class: "chat-name", text: name }), ": ", text);
          line.addEventListener("click", () => {
            modal.close();
            openWatch(item, { start: Math.max(0, offset - 2) });
          });
          return line;
        }));
    }));
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    timer = setTimeout(search, 350);
  });
  by.addEventListener("change", search);
}
