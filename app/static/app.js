// Фаза 0: проверка живости и просмотр того, что реально приходит в приёмник.
// Никакой логики расчёта здесь нет и не должно быть — она на бэкенде.

const $ = (id) => document.getElementById(id);

function setStatus(text, state) {
  const el = $("status");
  el.textContent = text;
  el.dataset.state = state;
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

async function loadHealth() {
  try {
    const res = await fetch("/api/health", { cache: "no-store" });
    const data = await res.json();
    setStatus(`API ok · БД ${data.db ? "ok" : "нет"} · ${data.view}`, "ok");
    $("team-name").textContent = data.team;
    $("foot-team").textContent = data.team;
    $("card-ingest").textContent = data.db
      ? "база поднята, приёмник пишет в лог"
      : "база недоступна — смотри логи контейнера";
    $("card-source").textContent = data.demo_mode
      ? "демо-режим: фикстуры"
      : "живой режим: база";
  } catch (err) {
    setStatus("API не отвечает", "bad");
    $("card-ingest").textContent = String(err);
  }
}

function renderRow(item) {
  const row = el("div", "row");

  const top = el("div", "row-top");
  top.append(el("span", "badge", item.status));
  if (item.status) top.lastChild.dataset.kind = item.status;
  top.append(el("span", null, `#${item.id}`));
  top.append(el("span", null, item.received_at));
  top.append(el("span", null, `${item.size} B`));
  if (item.delivery_id) top.append(el("span", null, `delivery ${item.delivery_id}`));
  if (item.error) top.append(el("span", null, item.error));
  row.append(top);

  const details = el("details");
  details.append(el("summary", null, "сырой запрос"));
  details.append(el("pre", null, item.raw || "(пусто)"));
  row.append(details);

  return row;
}

async function loadLog() {
  try {
    const res = await fetch("/api/ingest/log?limit=20", { cache: "no-store" });
    const data = await res.json();
    const box = $("log");
    box.replaceChildren();
    $("log-total").textContent = `всего: ${data.total}`;

    if (!data.items.length) {
      box.append(el("p", "muted", "Пока ничего не приходило."));
      return;
    }
    for (const item of data.items) box.append(renderRow(item));
  } catch (err) {
    $("log-total").textContent = "лог недоступен";
  }
}

async function tick() {
  await Promise.all([loadHealth(), loadLog()]);
}

tick();
setInterval(tick, 3000);
