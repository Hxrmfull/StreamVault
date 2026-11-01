/*
 * StreamVault, окно программы: поиск каналов по имени сразу на всех
 * площадках и окно «Добавить канал».
 */

import { PLATFORM, bridge, button, fillAvatar, fmt, h, isUrl, knownProfile, liveQualitySelect, openModal, platformName, safeUrl, setSrc, setText, siteInfo, state, t, toast } from "./common.js";
import { navigate, routeHash } from "./app.js";

export function openAddChannel() {
  const modal = openModal();
  const input = h("input", { type: "text", placeholder: t("Имя канала или ссылка на него"), spellcheck: "false" });
  let chosen = null;      // канал, выбранный из выдачи
  const chosenLine = h("div", { class: "chosen-channel" });
  chosenLine.hidden = true;
  // сразу стоит общее качество; не меняли — своего у канала нет
  const quality = liveQualitySelect(state.settings.quality);
  const autorecord = h("input", { type: "checkbox" });
  const error = h("div", { class: "error-text" });
  const submit = button(t("Добавить"), { kind: "primary", type: "submit" });
  const form = h("form", null,
    h("h2", { text: t("Добавить в избранное") }),
    h("div", { class: "field" }, h("label", null, t("Канал")), h("div", null, input), chosenLine,
      h("div", { class: "hint", text: t("Начни вводить имя — StreamVault найдёт каналы на Twitch, Kick, YouTube " +
                                        "и VK Video Live. Можно вставить и ссылку на канал.") })),
    h("div", { class: "field" }, h("label", null, t("Качество записи")), quality),
    h("div", { class: "field" },
      h("label", { class: "switch" }, autorecord, h("span", { class: "track" }), t("Автозапись")),
      h("div", { class: "hint", text: t("Записывать каждый эфир. Выключено — эфир можно записать " +
                                        "кнопкой на странице канала.") })),
    error,
    h("div", { class: "actions" }, button(t("Отмена"), { onclick: () => modal.close() }), submit));
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    setText(error, "");
    submit.disabled = true;
    try {
      const id = await bridge.call("add_channel", chosen ? chosen.id : input.value,
                                   quality.value === state.settings.quality ? null : quality.value,
                                   autorecord.checked,
                                   chosen ? knownProfile(chosen.id) : null);
      modal.close();
      toast(t("Канал добавлен"), "ok");
      navigate(routeHash("channel", id));
    } catch (problem) {
      setText(error, problem.message);
    } finally {
      submit.disabled = false;
    }
  });
  modal.body.append(form);
  attachChannelSearch(input, (channel) => {
    chosen = channel;
    input.value = channel.name;
    const avatar = h("span", { class: "avatar" });
    fillAvatar(avatar, channel.id, channel.platform);
    chosenLine.replaceChildren(avatar, h("span", { text: channel.name }),
      h("span", { class: "badge platform", text: platformName(channel.platform) }));
    chosenLine.hidden = false;
  });
  // поле поправили руками — выбор из выдачи больше не действует
  input.addEventListener("input", () => {
    chosen = null;
    chosenLine.hidden = true;
  });
  input.focus();
}

/*
 * Выдача каналов по имени со всех платформ под полем ввода: имя
 * набирают как есть, без kick:. Ссылки и «kick:имя» по-прежнему
 * открываются Enter без выбора. onPick(канал) — выбрали из выдачи.
 */
export const SEARCH_SHOWN = 3;    // каналов на платформу сразу; остальные — по «ещё»

export function attachChannelSearch(input, onPick) {
  const panel = h("div", { class: "search-results" });
  panel.hidden = true;
  input.parentNode.classList.add("search-anchor");
  input.parentNode.append(panel);
  let timer = null;
  let generation = 0;
  let rows = [];
  let active = -1;

  function hide() {
    panel.hidden = true;
    highlight(-1);
  }

  function highlight(index) {
    rows.forEach((row, i) => row.classList.toggle("active", i === index));
    active = index;
    if (rows[index]) rows[index].scrollIntoView({ block: "nearest" });
  }

  function pick(item) {
    state.profiles.set(item.id, { name: item.name, avatar: item.avatar });
    hide();
    onPick(item);
  }

  function resultRow(item) {
    const avatar = h("span", { class: "avatar" });
    if (safeUrl(item.avatar)) {
      const img = h("img", { alt: "", loading: "lazy" });
      setSrc(img, item.avatar);
      avatar.append(img);
    } else {
      // у Kick аватарок в выдаче нет — буква, как у каналов без аватарки
      avatar.append((item.name || "?").trim().charAt(0).toUpperCase() || "?");
      avatar.style.background = PLATFORM[item.platform].color;
    }
    const details = [];
    if (item.live) {
      details.push(h("span", { class: "live-text", text: item.viewers !== null && item.viewers !== undefined
        ? t("в эфире · {viewers}", { viewers: fmt.viewers(item.viewers) }) : t("в эфире") }));
    }
    if (item.followers !== null && item.followers !== undefined) details.push(fmt.followers(item.followers));
    if (item.game) details.push(item.game);
    const tracked = state.channels.get(item.id);
    const row = h("button", { type: "button", class: "search-row" }, avatar,
      h("span", { class: "search-row-text" },
        h("span", { class: "search-row-name", text: item.name }),
        h("span", { class: "search-row-sub" },
          ...details.flatMap((part, index) => index ? [" · ", part] : [part]))),
      tracked && tracked.favorite !== false && !tracked.temporary
        ? h("span", { class: "badge", text: t("в избранном") }) : null);
    row.addEventListener("mousedown", (event) => event.preventDefault());   // фокус остаётся в поле
    row.addEventListener("click", () => pick(item));
    return row;
  }

  function section(platform) {
    const list = h("div", { class: "search-list" }, h("div", { class: "spinner small" }));
    const dot = h("span", { class: "dot" });
    dot.style.background = PLATFORM[platform].color;
    const el = h("div", { class: "search-section" },
      h("div", { class: "search-platform" }, dot, platformName(platform)), list);
    return { el, list };
  }

  function collectRows() {
    rows = [...panel.querySelectorAll(".search-row")];
    if (active >= rows.length) highlight(-1);
  }

  function run(text) {
    const current = ++generation;
    rows = [];
    active = -1;
    panel.replaceChildren();
    panel.hidden = false;
    for (const platform of Object.keys(state.info.sites || {})) {
      const part = section(platform);
      panel.append(part.el);
      bridge.call("search_channels", platform, text).then((answer) => {
        if (current !== generation) return;
        const found = answer.results || [];
        part.list.replaceChildren(...found.slice(0, SEARCH_SHOWN).map(resultRow));
        if (found.length > SEARCH_SHOWN) {
          const more = h("button", { type: "button", class: "search-more",
                                     text: t("ещё {count}", { count: found.length - SEARCH_SHOWN }) });
          more.addEventListener("mousedown", (event) => event.preventDefault());
          more.addEventListener("click", () => {
            more.replaceWith(...found.slice(SEARCH_SHOWN).map(resultRow));
            collectRows();
          });
          part.list.append(more);
        }
        if (!found.length) {
          part.list.append(h("div", { class: "search-empty", text: answer.error
            ? t("не ответила: {error}", { error: answer.error })
            : siteInfo(platform).search_note || t("ничего не нашлось") }));
        }
        collectRows();
      }).catch((error) => {
        if (current !== generation) return;
        part.list.replaceChildren(h("div", { class: "search-empty", text: error.message }));
      });
    }
  }

  // имя, а не ссылка и не «платформа:имя» — их открывает Enter, как раньше
  function searchable(text) {
    return text.length >= 2 && !isUrl(text) && !/^[a-z]+:/i.test(text);
  }

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const text = input.value.trim();
    if (!searchable(text)) {
      generation++;
      hide();
      return;
    }
    timer = setTimeout(() => run(text), 350);
  });
  input.addEventListener("keydown", (event) => {
    if (panel.hidden) return;
    if (event.key === "ArrowDown" || event.key === "ArrowUp") {
      event.preventDefault();
      if (!rows.length) return;
      // по кругу: за последней строкой — снова поле ввода (-1)
      let next = active + (event.key === "ArrowDown" ? 1 : -1);
      if (next >= rows.length) next = -1;
      if (next < -1) next = rows.length - 1;
      highlight(next);
    } else if (event.key === "Enter" && active >= 0) {
      event.preventDefault();
      rows[active].click();
    } else if (event.key === "Escape") {
      hide();
    }
  });
  input.addEventListener("focus", () => {
    if (panel.childElementCount && searchable(input.value.trim())) panel.hidden = false;
  });
  input.addEventListener("blur", () => setTimeout(hide, 150));
  // перешли на другой экран («назад», ссылка в уведомлении) — выдача не должна висеть поверх
  window.addEventListener("hashchange", hide);
  return { hide, clear: () => { generation++; hide(); panel.replaceChildren(); } };
}
