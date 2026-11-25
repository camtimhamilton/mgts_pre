// Один экран: общая картина сверху, зона риска слева, объяснение справа.
// Данные только из API; арифметику здесь не придумываем — показываем то, что посчитал бэкенд.

const $ = (selector) => document.querySelector(selector);

const BAND_LABEL = { low: "стабильно", watch: "наблюдение", critical: "критично" };
const BAND_TONE = { low: "low", watch: "watch", critical: "critical" };
const ROLE_LABEL = { intern: "стажёр", mentor: "наставник" };
const FACTOR_SHORT = {
  motivation: "Мотивация",
  clarity: "Ясность задач",
  mentor: "Наставник",
  workload: "Нагрузка",
  inclusion: "Включённость",
  future: "Будущее",
};
const MONTHS = ["M1", "M2", "M3", "M4", "M5", "M6"];

const state = {
  month: null,
  months: [],
  program: null,
  pairs: [],
  card: null,
  selectedId: null,
  sort: "urgency",
  filter: "all",
  query: "",
  source: "db",
  view: "hr",
  logTimer: null,
  refreshTimer: null,
};

// ---------------------------------------------------------------- утилиты

class ApiError extends Error {
  constructor(status, body) {
    super(`HTTP ${status}`);
    this.status = status;
    this.body = body;
  }
}

async function api(path) {
  const res = await fetch(path, { cache: "no-store" });
  const source = (res.headers.get("x-data-source") || "").toLowerCase();
  let body = null;
  try {
    body = await res.json();
  } catch {
    body = null;
  }
  if (!res.ok) throw new ApiError(res.status, body);
  return { body, source };
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined && text !== null) node.textContent = String(text);
  return node;
}

function svg(tag, attrs = {}) {
  const node = document.createElementNS("http://www.w3.org/2000/svg", tag);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  return node;
}

function initials(name) {
  return (name || "?")
    .split(/[\s-]+/)
    .filter(Boolean)
    .slice(0, 2)
    .map((part) => part[0].toUpperCase())
    .join("");
}

function hue(name) {
  let hash = 0;
  for (const char of name || "x") hash = (hash * 31 + char.charCodeAt(0)) % 360;
  return hash;
}

function avatarStyle(name) {
  const h = hue(name);
  return `background: hsl(${h} 42% 62%); color: #0b0f13;`;
}

function deltaChip(delta) {
  const value = Number(delta) || 0;
  const sign = value > 0 ? "up" : value < 0 ? "down" : "flat";
  const arrow = value > 0 ? "↑" : value < 0 ? "↓" : "→";
  const text = value === 0 ? "0" : `${arrow} ${Math.abs(value)}`;
  const chip = el("span", "delta", text);
  chip.dataset.sign = sign;
  chip.title = `изменение к прошлому месяцу: ${value > 0 ? "+" : ""}${value} п.п.`;
  return chip;
}

function bandBadge(band, prefix = "Уровень") {
  const badge = el("span", "badge", `${prefix}: ${BAND_LABEL[band] || band}`);
  badge.dataset.tone = BAND_TONE[band] || "watch";
  return badge;
}

function status(text, stateName) {
  const node = $("#status");
  node.textContent = text;
  node.dataset.state = stateName || "";
}

function toast(message) {
  const node = $("#toast");
  node.textContent = message;
  node.hidden = false;
  clearTimeout(toast._timer);
  toast._timer = setTimeout(() => {
    node.hidden = true;
  }, 4000);
}

function fmtSeconds(value) {
  if (value === null || value === undefined) return "—";
  if (value < 60) return `${value.toFixed(1)} с`;
  return `${Math.round(value)} с`;
}

function fmtDateTime(value) {
  if (!value) return "";
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("ru-RU", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" });
}

// ---------------------------------------------------------------- загрузка

async function loadProgram(month) {
  const query = month ? `?month=${encodeURIComponent(month)}` : "";
  const [program, pairs] = await Promise.all([api(`/api/program${query}`), api(`/api/pairs${query}`)]);

  state.program = program.body;
  state.pairs = pairs.body.items || [];
  state.month = program.body.month;
  state.months = program.body.months?.length ? program.body.months : MONTHS;
  state.source = program.source || "db";
}

async function loadCard(pairId) {
  const { body } = await api(`/api/pairs/${pairId}?month=${encodeURIComponent(state.month)}`);
  state.card = body;
  state.selectedId = pairId;
}

async function refresh() {
  try {
    await loadProgram(state.month);
    if (state.selectedId && state.pairs.some((pair) => pair.id === state.selectedId)) {
      await loadCard(state.selectedId);
    } else {
      state.selectedId = null;
      state.card = null;
    }
    renderAll();
    status(`обновлено ${new Date().toLocaleTimeString("ru-RU")}`, "ok");
  } catch (error) {
    status("не удалось обновить", "bad");
    showCardError(error);
  }
}

function scheduleRefresh() {
  clearTimeout(state.refreshTimer);
  state.refreshTimer = setTimeout(() => refresh(), 400);
}

// ---------------------------------------------------------------- KPI

function renderKpi() {
  const box = $("#kpi");
  box.replaceChildren();
  if (!state.program) return;

  const kpi = state.program.kpi || {};
  const total = kpi.total_pairs || 0;

  const chips = [
    {
      key: "risk",
      label: "в зоне риска",
      value: kpi.at_risk ?? 0,
      suffix: `из ${total}`,
      tone: (kpi.at_risk || 0) > 0 ? "bad" : "ok",
      filter: "risk",
    },
    { key: "avg", label: "средний индекс", value: kpi.avg_risk ?? 0, tone: toneOfScore(kpi.avg_risk) },
    {
      key: "gap",
      label: "разрыв восприятия",
      value: kpi.gap_pairs ?? 0,
      suffix: "пар",
      tone: (kpi.gap_pairs || 0) > 0 ? "warn" : "",
      filter: "gap",
    },
    {
      key: "submitted",
      label: "сдано анкет",
      value: `${kpi.submitted?.done ?? 0}`,
      suffix: `из ${kpi.submitted?.expected ?? total * 2}`,
    },
    { key: "signal", label: "последний сигнал", value: fmtSeconds(kpi.last_signal_sec), tone: "" },
  ];

  for (const chip of chips) {
    const node = el("button", "kpi-chip");
    node.type = "button";
    node.append(el("span", "kpi-label", chip.label));

    const value = el("span", "kpi-value", chip.value);
    if (chip.tone) value.dataset.tone = chip.tone;
    if (chip.suffix) {
      const small = el("small", null, chip.suffix);
      value.append(small);
    }
    node.append(value);

    if (chip.filter) {
      node.dataset.clickable = "1";
      if (state.filter === chip.filter) node.dataset.active = "1";
      node.addEventListener("click", () => setFilter(chip.filter));
    }

    box.append(node);
  }

  renderExec();
}

function toneOfScore(value) {
  if (value === null || value === undefined) return "";
  if (value >= 60) return "bad";
  if (value >= 35) return "warn";
  return "ok";
}

function renderExec() {
  const extra = $("#kpi-extra");
  if (state.view !== "exec") {
    extra.style.display = "none";
    return;
  }
  extra.style.display = "flex";

  const bands = state.program?.bands || { low: 0, watch: 0, critical: 0 };
  const total = Math.max(1, bands.low + bands.watch + bands.critical);
  const bar = $("#bands-bar");
  bar.replaceChildren();
  for (const key of ["low", "watch", "critical"]) {
    const segment = el("span", key);
    segment.style.width = `${(bands[key] / total) * 100}%`;
    segment.title = `${BAND_LABEL[key]}: ${bands[key]}`;
    bar.append(segment);
  }
  $("#bands-legend").textContent = `стабильно ${bands.low} · наблюдение ${bands.watch} · критично ${bands.critical}`;

  const withGap = state.pairs.filter((pair) => (pair.gap || 0) >= 4);
  const without = state.pairs.filter((pair) => (pair.gap || 0) < 4);
  const avg = (items) => (items.length ? items.reduce((sum, item) => sum + item.risk, 0) / items.length : 0);
  $("#hypothesis").innerHTML = "";
  $("#hypothesis").append(
    document.createTextNode("гипотеза «разрыв объясняет риск»: средний индекс "),
    el("b", null, avg(withGap).toFixed(0)),
    document.createTextNode(` у ${withGap.length} пар с разрывом против `),
    el("b", null, avg(without).toFixed(0)),
    document.createTextNode(` у ${without.length} без него`)
  );
}

// ---------------------------------------------------------------- список

function visiblePairs() {
  let items = [...state.pairs];
  if (state.query) {
    const needle = state.query.toLowerCase();
    items = items.filter((pair) =>
      `${pair.intern_name} ${pair.mentor_name || ""} ${pair.direction || ""}`.toLowerCase().includes(needle)
    );
  }
  if (state.filter === "risk") items = items.filter((pair) => pair.risk >= 60 || pair.delta >= 10);
  if (state.filter === "gap") items = items.filter((pair) => (pair.gap || 0) >= 4);
  if (state.filter === "missing") items = items.filter((pair) => Boolean(pair.missing));

  // Пары с неполными данными всегда внизу: высокий индекс из двух ответов
  // не должен поднимать их выше тех, о ком известно больше.
  const last = (pair) => (pair.data_status === "insufficient" ? 1 : 0);
  const sorters = {
    urgency: (a, b) => last(a) - last(b) || b.urgency - a.urgency,
    risk: (a, b) => last(a) - last(b) || b.risk - a.risk,
    delta: (a, b) => last(a) - last(b) || b.delta - a.delta,
  };
  return items.sort(sorters[state.sort] || sorters.urgency);
}

function renderList() {
  const list = $("#list");
  list.replaceChildren();
  const items = visiblePairs();

  if (!items.length) {
    list.append(el("div", "list-empty", "Никого нет под этим фильтром."));
    return;
  }

  for (const pair of items) {
    const row = el("button", "row-person");
    row.type = "button";
    row.setAttribute("aria-selected", String(pair.id === state.selectedId));
    row.addEventListener("click", () => selectPair(pair.id));

    const face = el("div", "avatar", initials(pair.intern_name));
    face.setAttribute("style", avatarStyle(pair.intern_name));

    const main = el("div", "row-main");
    const name = el("div", "row-name", pair.intern_name);
    const sub = el("div", "row-sub", [pair.mentor_name, pair.direction].filter(Boolean).join(" · ") || "наставник не указан");
    main.append(name, sub);

    const flags = el("div", "row-flags");
    if (pair.missing) {
      const flag = el("span", "flag", pair.missing === "mentor" ? "нет анкеты наставника" : "нет анкеты стажёра");
      flag.dataset.kind = "missing";
      flags.append(flag);
    }
    if ((pair.gap || 0) >= 4) {
      const flag = el("span", "flag", `разрыв ${pair.gap}`);
      flag.dataset.kind = "gap";
      flags.append(flag);
    }
    if (pair.data_status === "insufficient") {
      const flag = el("span", "flag", "мало данных");
      flag.dataset.kind = "missing";
      flags.append(flag);
    } else if (pair.data_status === "partial" && !pair.missing) {
      const flag = el("span", "flag", "неполные данные");
      flag.dataset.kind = "missing";
      flags.append(flag);
    }
    if (flags.childElementCount) main.append(flags);

    const right = el("div", "row-right");
    right.append(sparkline(pair.sparkline || [], pair.band));

    const score = el("div", "row-risk", pair.risk);
    score.dataset.band = pair.band;

    const column = el("div");
    column.style.display = "flex";
    column.style.flexDirection = "column";
    column.style.alignItems = "flex-end";
    column.style.gap = "3px";
    column.append(score, deltaChip(pair.delta));
    right.append(column);

    row.append(face, main, right);
    list.append(row);
  }
}

function sparkline(values, band) {
  const width = 64;
  const height = 20;
  const node = svg("svg", { class: "spark", width, height, viewBox: `0 0 ${width} ${height}`, "aria-hidden": "true" });
  if (!values.length) return node;

  const step = values.length > 1 ? (width - 4) / (values.length - 1) : 0;
  const points = values.map((value, index) => {
    const x = 2 + index * step;
    const y = height - 2 - (Math.max(0, Math.min(100, value)) / 100) * (height - 4);
    return [x, y];
  });

  const path = points.map(([x, y], index) => `${index ? "L" : "M"}${x.toFixed(1)} ${y.toFixed(1)}`).join(" ");
  node.append(
    svg("path", {
      d: path,
      fill: "none",
      stroke: band === "critical" ? "#e5534b" : band === "watch" ? "#e3b341" : "#4cc9a4",
      "stroke-width": "1.6",
      "stroke-linejoin": "round",
    })
  );

  const [lastX, lastY] = points[points.length - 1];
  node.append(svg("circle", { cx: lastX, cy: lastY, r: 2, fill: band === "critical" ? "#e5534b" : band === "watch" ? "#e3b341" : "#4cc9a4" }));
  return node;
}

function setFilter(filter) {
  state.filter = state.filter === filter ? "all" : filter;
  renderKpi();
  renderList();
}

function renderFilters() {
  for (const button of document.querySelectorAll("#filters button")) {
    button.classList.toggle("active", button.dataset.filter === state.filter);
  }
  for (const button of document.querySelectorAll("#sort button")) {
    button.classList.toggle("active", button.dataset.sort === state.sort);
  }
}

// ---------------------------------------------------------------- теплокарта

function renderHeatmap() {
  const box = $("#heatmap");
  box.replaceChildren();
  const months = MONTHS;
  const rows = state.program?.heatmap || [];
  if (!rows.length) {
    box.append(el("p", "muted", "нет данных"));
    return;
  }

  box.append(el("span", "hm-head"));
  for (const month of months) box.append(el("span", "hm-head", month.slice(1)));

  for (const row of rows) {
    const name = el("span", "hm-name", row.name);
    name.title = row.name;
    box.append(name);

    const byMonth = new Map((row.cells || []).map((cell) => [cell.month, cell.risk]));
    for (const month of months) {
      const risk = byMonth.get(month);
      const cell = el("button", "hm-cell");
      cell.type = "button";
      if (risk === undefined) {
        cell.dataset.empty = "1";
        cell.tabIndex = -1;
      } else {
        cell.dataset.band = bandOf(risk);
        cell.title = `${row.name}, ${month}: риск ${risk}`;
        if (month === state.month) cell.dataset.current = "1";
        cell.addEventListener("click", async () => {
          if (month !== state.month) await switchMonth(month);
          await selectPair(row.pair_id);
        });
      }
      box.append(cell);
    }
  }
}

function bandOf(risk) {
  if (risk >= 60) return "critical";
  if (risk >= 35) return "watch";
  return "low";
}

function renderCauses() {
  const box = $("#causes");
  box.replaceChildren();
  const causes = state.program?.top_causes || [];
  if (!causes.length) {
    box.append(el("p", "muted", "пока нет данных"));
    return;
  }
  const max = Math.max(...causes.map((cause) => cause.count), 1);

  for (const cause of causes) {
    const row = el("div", "cause-row");
    row.append(el("span", "cause-label", cause.label));

    const bar = el("div", "cause-bar");
    const fill = el("span");
    fill.style.width = `${(cause.count / max) * 100}%`;
    bar.append(fill);

    row.append(bar, el("span", "cause-count", `${cause.count} из ${state.pairs.length}`));
    box.append(row);
  }
}

// ---------------------------------------------------------------- карточка

function renderCard() {
  const box = $("#card");
  box.replaceChildren();
  const card = state.card;

  if (!card) {
    box.append(emptyCard());
    return;
  }

  box.append(hero(card));

  if (card.conversation_required) {
    box.append(
      el(
        "div",
        "alert",
        "В ответе есть прямое намерение уйти. Это не оценка человека, а повод поговорить: "
          + "уточнить ситуацию и предложить помощь, не обещая оффер."
      )
    );
  }

  if (card.degraded) {
    box.append(el("div", "degraded", "Модель была недоступна: текст собран по шаблонам, числа посчитаны обычно."));
  }

  box.append(summaryBlock(card));
  box.append(gapBlock(card));
  box.append(reasonsBlock(card));
  box.append(stepsBlock(card));
  box.append(chartBlock(card));
  box.append(metaLine(card));
}

function emptyCard() {
  const node = el("div", "empty");
  node.append(el("div", "empty-icon", "◎"));
  node.append(el("h2", null, "Выберите человека"));
  node.append(el("p", "muted", "Слева — список пар, отсортированный по срочности."));
  return node;
}

function hero(card) {
  const node = el("div", "card-hero");

  const face = el("div", "hero-avatar", initials(card.intern_name));
  face.setAttribute("style", avatarStyle(card.intern_name));

  const info = el("div");
  info.append(el("h1", "hero-name", card.intern_name));
  info.append(
    el(
      "div",
      "hero-meta",
      [card.mentor_name ? `наставник: ${card.mentor_name}` : "наставник не указан", card.direction]
        .filter(Boolean)
        .join(" · ")
    )
  );
  info.append(
    el("div", "hero-meta", `месяц ${card.program?.month_current || "?"} из ${card.program?.months_total || 6}`)
  );

  const badges = el("div", "hero-badges");
  badges.append(bandBadge(card.band));
  badges.append(bandBadge(card.trend?.key === "fast_growth" ? "critical" : card.trend?.key === "growing" ? "watch" : "low", "Тренд"));
  badges.lastChild.textContent = `Тренд: ${card.trend?.label || "—"}`;

  if (card.data_status && card.data_status !== "complete") {
    const badge = el(
      "span",
      "badge",
      card.data_status === "insufficient" ? "Мало данных" : "Неполные данные"
    );
    badge.dataset.tone = "watch";
    badge.title = `наблюдено факторов: ${(card.factors || []).length} из 6`;
    badges.append(badge);
  }

  if (card.score_origin && card.score_origin !== "answer") {
    const badge = el(
      "span",
      "badge",
      card.score_origin === "estimated" ? "оценки восстановлены по тексту" : "оценки из импорта"
    );
    badge.dataset.dashed = "1";
    badges.append(badge);
  }
  info.append(badges);

  const score = el("div", "hero-score");
  const risk = el("div", "hero-risk", card.risk);
  risk.dataset.band = card.band;
  score.append(risk);
  score.append(el("small", null, "индекс риска"));
  const delta = el("div", "hero-delta");
  delta.append(deltaChip(card.delta));
  score.append(delta);

  node.append(face, info, score);
  return node;
}

function summaryBlock(card) {
  const block = el("section", "block");
  const head = el("h3");
  head.append(el("span", null, "Краткое резюме"));
  const button = el("button", "linklike", "оригиналы ответов →");
  button.type = "button";
  button.addEventListener("click", () => openOriginals(card.id));
  head.append(button);
  block.append(head);
  block.append(el("p", "summary-text", card.summary || "Модель не дала резюме, смотрите оригиналы."));

  if (card.signals?.length) {
    const signals = el("div", "signals");
    signals.style.marginTop = "10px";
    for (const signal of card.signals) {
      const chip = el("span", "signal-chip", `сигнал: ${signal.label}`);
      chip.title = signal.quote || "";
      signals.append(chip);
    }
    block.append(signals);
  }
  return block;
}

function gapBlock(card) {
  const block = el("section", "block");
  block.append(el("h3", null, "Расхождение оценок"));

  if (card.gap_comment) block.append(el("p", "summary-text", card.gap_comment));

  const grid = el("div", "gap-grid");
  grid.style.marginTop = "10px";

  for (const factor of card.factors || []) {
    const row = el("div", "gap-row");
    const name = el("span", "gap-name", factor.label || FACTOR_SHORT[factor.key] || factor.key);
    if (factor.state === "conflicting") {
      name.append(el("span", "gap-conflict", " ⚑"));
      name.title = "стажёр и наставник расходятся больше чем на 4 балла";
    }
    row.append(name);

    const bars = el("div", "gap-bars");
    const intern = el("div", "gap-bar");
    intern.dataset.side = "intern";
    const internFill = el("span");
    internFill.style.width = `${((factor.score_intern ?? 0) / 10) * 100}%`;
    intern.append(internFill);

    const mentor = el("div", "gap-bar");
    mentor.dataset.side = "mentor";
    const mentorFill = el("span");
    mentorFill.style.width = `${((factor.score_mentor ?? 0) / 10) * 100}%`;
    mentor.append(mentorFill);

    bars.append(intern, mentor);

    const values = el(
      "span",
      "gap-values",
      `${factor.score_intern ?? "—"} / ${factor.score_mentor ?? "—"}`
    );
    if ((factor.gap || 0) >= 4) values.dataset.gap = "1";

    row.append(bars, values);
    grid.append(row);
  }
  block.append(grid);

  const legend = el("div", "gap-legend");
  legend.append(el("span", null, "стажёр"), el("span", null, "наставник"));
  legend.firstChild.prepend(el("i", "i-intern"));
  legend.lastChild.prepend(el("i", "i-mentor"));
  block.append(legend);
  return block;
}

function reasonsBlock(card) {
  const block = el("section", "block");
  const head = el("h3", null, "Почему такой индекс");
  block.append(head);

  const states = new Map((card.factors || []).map((factor) => [factor.key, factor.state]));
  for (const reason of card.reasons || []) {
    const row = el("div", "reason");
    row.append(el("div", "reason-points", `+${reason.points}`));
    const body = el("div");
    const title = el("div", "reason-title", reason.label || FACTOR_SHORT[reason.key] || reason.key);
    if (states.get(reason.key) === "conflicting") {
      const mark = el("span", "reason-conflict", "стороны расходятся");
      title.append(mark);
    }
    body.append(title);
    body.append(el("div", "reason-text", reason.text || ""));
    if (reason.quote) body.append(el("blockquote", "blockquote", `«${reason.quote}»`));
    row.append(body);
    block.append(row);
  }

  if (card.signals?.length) {
    for (const signal of card.signals) {
      const row = el("div", "reason");
      row.append(el("div", "reason-points", `+${Math.round(signal.points || 0)}`));
      const body = el("div");
      body.append(el("div", "reason-title", `Сигнал: ${signal.label}`));
      body.append(el("div", "reason-text", "выделено из текста ответа"));
      if (signal.quote) body.append(el("blockquote", "blockquote", `«${signal.quote}»`));
      row.append(body);
      block.append(row);
    }
  }

  const fallbackBase = (card.factors || []).reduce((sum, factor) => sum + (factor.points || 0), 0);
  const base = card.base ?? fallbackBase;
  const total = base + (card.gap_penalty ?? 0) + (card.text_penalty ?? 0);
  const arithmetic = el(
    "p",
    "meta-line",
    total > card.risk
      ? `складывается: ${base} + ${card.gap_penalty} + ${card.text_penalty} = ${total}, ограничено 100`
      : `складывается: ${base} + ${card.gap_penalty} + ${card.text_penalty} = ${card.risk}`
  );
  block.append(arithmetic);
  return block;
}

function stepsBlock(card) {
  const block = el("section", "block");
  const steps = card.steps || [];
  const head = el("h3");
  head.append(el("span", null, "Что сделать"));
  if (steps.length) {
    const sum = steps.reduce((total, step) => total + (step.reduction || 0), 0);
    head.append(el("span", "muted", `может снизить на ${Math.round(sum)} п.п.`));
  }
  block.append(head);

  for (const step of steps) {
    const row = el("div", "step");
    row.append(el("div", "step-points", `−${Math.round(step.reduction)}`));
    const body = el("div");
    body.append(el("div", "step-title", step.text));
    const meta = el("div", "step-meta");
    meta.append(el("span", null, `владелец: ${step.owner || "—"}`), el("span", null, `срок: ${step.due || "—"}`));
    body.append(meta);
    row.append(body);
    block.append(row);
  }
  return block;
}

function chartBlock(card) {
  const block = el("section", "block");
  block.append(el("h3", null, "Динамика и сценарии"));
  const history = card.history || [];
  const base = card.scenarios?.base || [];
  const actions = card.scenarios?.with_actions || [];

  if (!history.length) {
    block.append(el("p", "muted", "нет данных по месяцам"));
    return block;
  }

  const wrap = el("div", "chart-wrap");
  wrap.append(buildChart(history, base, actions));
  block.append(wrap);

  const legend = el("div", "legend");
  const l1 = el("span");
  l1.append(el("i", "l-history"), document.createTextNode("факт"));
  const l2 = el("span");
  l2.append(el("i", "l-base"), document.createTextNode("если ничего не менять"));
  const l3 = el("span");
  l3.append(el("i", "l-actions"), document.createTextNode("если выполнить шаги"));
  legend.append(l1, l2, l3);
  block.append(legend);

  if (base.length) {
    const note = el("p", "scenario-note");
    note.append(
      document.createTextNode(
        `на ${base[0].month}: без действий ${base[0].risk} (${base[0].low}–${base[0].high}), со шагами ${actions[0]?.risk ?? "—"}`
      )
    );
    if (card.scenarios?.event) {
      note.append(document.createTextNode(" · "));
      note.append(el("b", null, card.scenarios.event));
    }
    block.append(note);
  } else {
    block.append(el("p", "scenario-note", "месяц последний в программе — прогноза на следующий нет."));
  }

  return block;
}

function buildChart(history, base, actions) {
  const width = 760;
  const height = 240;
  const pad = { left: 30, right: 12, top: 12, bottom: 24 };
  const months = [...history.map((point) => point.month), ...base.map((point) => point.month)];
  const innerWidth = width - pad.left - pad.right;
  const innerHeight = height - pad.top - pad.bottom;

  const count = Math.max(months.length, 2);
  const x = (index) => pad.left + (innerWidth * index) / (count - 1);
  const y = (value) => pad.top + innerHeight * (1 - Math.max(0, Math.min(100, value)) / 100);
  const node = svg("svg", { class: "chart", viewBox: `0 0 ${width} ${height}`, role: "img", "aria-label": "Динамика риска и сценарии" });

  node.append(svg("rect", { class: "band-critical", x: pad.left, y: y(100), width: innerWidth, height: Math.abs(y(60) - y(100)) }));
  node.append(svg("rect", { class: "band-watch", x: pad.left, y: y(60), width: innerWidth, height: Math.abs(y(35) - y(60)) }));

  for (const value of [0, 25, 50, 75, 100]) {
    node.append(svg("line", { class: "axis", x1: pad.left, x2: width - pad.right, y1: y(value), y2: y(value) }));
    const label = svg("text", { x: pad.left - 6, y: y(value) + 3, "text-anchor": "end" });
    label.textContent = String(value);
    node.append(label);
  }

  months.forEach((month, index) => {
    const label = svg("text", { x: x(index), y: height - 6, "text-anchor": "middle" });
    label.textContent = month;
    node.append(label);
  });

  if (base.length) {
    const lastIndex = history.length - 1;
    const startX = x(lastIndex);
    const startY = y(history[lastIndex].risk);

    const upper = base.map((point, index) => `${x(lastIndex + 1 + index)},${y(point.high)}`);
    const lower = [...base].reverse().map((point, index) => `${x(lastIndex + base.length - index)},${y(point.low)}`);
    node.append(
      svg("polygon", {
        class: "base-band",
        points: [`${startX},${startY}`, ...upper, ...lower].join(" "),
      })
    );

    node.append(
      svg("path", {
        class: "base",
        d: `M${startX} ${startY} ` + base.map((point, index) => `L${x(lastIndex + 1 + index)} ${y(point.risk)}`).join(" "),
      })
    );

    if (actions.length) {
      const actionUpper = actions.map((point, index) => `${x(lastIndex + 1 + index)},${y(point.high)}`);
      const actionLower = [...actions].reverse().map((point, index) => `${x(lastIndex + actions.length - index)},${y(point.low)}`);
      node.append(
        svg("polygon", {
          class: "actions-band",
          points: [`${startX},${startY}`, ...actionUpper, ...actionLower].join(" "),
        })
      );
      node.append(
        svg("path", {
          class: "actions",
          d: `M${startX} ${startY} ` + actions.map((point, index) => `L${x(lastIndex + 1 + index)} ${y(point.risk)}`).join(" "),
        })
      );
    }
  }

  const historyPath = history.map((point, index) => `${index ? "L" : "M"}${x(index)} ${y(point.risk)}`).join(" ");
  node.append(svg("path", { class: "history", d: historyPath }));
  history.forEach((point, index) => {
    node.append(svg("circle", { class: "history-dot", cx: x(index), cy: y(point.risk), r: 3.2 }));
  });

  if (base.length) {
    const marker = svg("line", {
      class: "marker",
      x1: x(history.length - 1),
      x2: x(history.length - 1),
      y1: pad.top,
      y2: height - pad.bottom,
    });
    node.insertBefore(marker, node.firstChild);
  }

  return node;
}

function metaLine(card) {
  const parts = [];
  if (card.observation) {
    parts.push(`наблюдений: ${card.observation.months}`);
    parts.push(`с двумя анкетами: ${card.observation.with_both_forms}`);
  }
  if (typeof card.coverage === "number") {
    parts.push(`данные: ${(card.factors || []).length} из 6 факторов`);
  }
  if (card.signal_seconds !== null && card.signal_seconds !== undefined) {
    parts.push(`сигнал от формы: ${fmtSeconds(card.signal_seconds)}`);
  }
  return el("p", "meta-line", parts.join(" · "));
}

// ---------------------------------------------------------------- действия

async function selectPair(pairId) {
  try {
    await loadCard(pairId);
    renderList();
    renderCard();
    $("#card").scrollTop = 0;
  } catch (error) {
    showCardError(error);
  }
}

async function switchMonth(month) {
  if (month === state.month) return;
  try {
    state.card = null;
    state.selectedId = null;
    await loadProgram(month);
    $("#month").value = state.month;
    renderAll();
  } catch (error) {
    status("месяц не загрузился", "bad");
  }
}

function showCardError(error) {
  const box = $("#card");
  box.replaceChildren();
  const node = el("div", "empty");
  node.append(el("div", "empty-icon", "⚠"));
  node.append(el("h2", null, "Не удалось загрузить карточку"));
  node.append(el("p", "muted", error instanceof ApiError ? `сервер ответил ${error.status}` : String(error)));
  box.append(node);
}

function renderAll() {
  renderFilters();
  renderKpi();
  renderList();
  renderHeatmap();
  renderCauses();
  renderCard();
  renderMonthSelect();
  renderSource();
}

function renderMonthSelect() {
  const select = $("#month");
  select.replaceChildren();
  for (const month of MONTHS) {
    const option = el("option", null, month);
    option.value = month;
    option.disabled = !state.months.includes(month);
    if (month === state.month) option.selected = true;
    select.append(option);
  }
  $("#month-hint").textContent = state.months.length ? `${state.months.length} мес. с данными` : "нет данных";
}

function renderSource() {
  const note = $("#source-note");
  if (state.source === "fixture") {
    note.textContent = "демо-данные из фикстур — в базе пока пусто";
    note.style.color = "var(--warn)";
  } else {
    note.textContent = `данные из базы · ${state.pairs.length} пар`;
    note.style.color = "";
  }
  $("#foot-team").textContent = $("#team-name").textContent;
}

// ---------------------------------------------------------------- ящики

function openDrawer(id) {
  $("#scrim").hidden = false;
  $(`#${id}`).hidden = false;
}

function closeDrawers() {
  $("#scrim").hidden = true;
  $("#originals").hidden = true;
  $("#log").hidden = true;
  clearInterval(state.logTimer);
  state.logTimer = null;
}

async function openOriginals(pairId) {
  openDrawer("originals");
  const body = $("#originals-body");
  body.replaceChildren(el("p", "muted", "загрузка…"));

  try {
    const { body: data } = await api(`/api/pairs/${pairId}/submissions`);
    const items = (data.items || []).filter((item) => item.month === state.month);
    const relevant = items.length ? items : (data.items || []).slice(-2);
    body.replaceChildren();

    if (!relevant.length) {
      body.append(el("p", "muted", "ответов нет"));
      return;
    }

    for (const role of ["intern", "mentor"]) {
      const section = el("div", "original-role");
      const item = relevant.find((entry) => entry.role === role);
      section.append(el("h4", null, ROLE_LABEL[role]));

      if (!item) {
        section.append(el("p", "muted", "анкета не сдана"));
        body.append(section);
        continue;
      }

      section.append(el("div", "original-time", `${item.month} · ${fmtDateTime(item.received_at)} · ${item.source}`));

      const grid = el("div", "answer-grid");
      for (const [key, label] of Object.entries(FACTOR_SHORT)) {
        const value = item.answers?.[key];
        const cell = el("div", "answer-cell");
        cell.append(el("span", null, label), el("span", null, value === undefined ? "—" : `${value}/10`));
        grid.append(cell);
      }
      section.append(grid);

      section.append(el("div", "original-label", "что изменилось к лучшему"));
      section.append(el("p", "original-text", item.open?.q_better || "—"));
      section.append(el("div", "original-label", "что мешает"));
      section.append(el("p", "original-text", item.open?.q_barrier || "—"));

      body.append(section);
    }
  } catch (error) {
    body.replaceChildren(el("p", "muted", `не удалось загрузить: ${error}`));
  }
}

async function openLog() {
  openDrawer("log");
  const load = async () => {
    try {
      const { body } = await api("/api/ingest/log?limit=12");
      const box = $("#log-body");
      box.replaceChildren();
      if (!body.items?.length) {
        box.append(el("p", "muted", "пока ничего не приходило"));
        return;
      }
      for (const item of body.items) {
        const row = el("div", "log-row");
        const head = el("div", "log-head");
        head.append(el("span", null, `#${item.id}`), el("span", null, item.status), el("span", null, item.received_at));
        if (item.delivery_id) head.append(el("span", null, `delivery ${item.delivery_id}`));
        row.append(head);
        const details = el("details");
        details.append(el("summary", "muted", "сырой запрос"));
        const raw = el("pre", "log-raw", item.raw || "");
        details.append(raw);
        row.append(details);
        box.append(row);
      }
    } catch {
      $("#log-body").replaceChildren(el("p", "muted", "журнал недоступен"));
    }
  };
  await load();
  clearInterval(state.logTimer);
  state.logTimer = setInterval(load, 3000);
}

// ---------------------------------------------------------------- SSE

function connectEvents() {
  const source = new EventSource("/api/events");

  source.addEventListener("snapshot.ready", (event) => {
    try {
      const data = JSON.parse(event.data);
      toast(`Новый ответ: ${data.month}, риск ${data.risk}`);
    } catch {
      toast("Новый ответ обработан");
    }
    scheduleRefresh();
  });

  source.addEventListener("program.ready", () => {
    toast("Импорт пересчитан");
    scheduleRefresh();
  });

  source.onerror = () => status("нет связи, переподключаюсь", "bad");
  source.onopen = () => status(`обновлено ${new Date().toLocaleTimeString("ru-RU")}`, "ok");
}

// ---------------------------------------------------------------- старт

async function boot() {
  const { body: health } = await api("/api/health");
  state.view = health.view || "hr";
  document.body.dataset.view = state.view;
  $("#team-name").textContent = health.team || "ПУЛЬС";
  $("#view-tag").textContent = state.view === "exec" ? "режим руководителя" : "мониторинг стажировки";

  $("#month").addEventListener("change", (event) => switchMonth(event.target.value));
  $("#search").addEventListener("input", (event) => {
    state.query = event.target.value.trim();
    renderList();
  });
  $("#sort").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-sort]");
    if (!button) return;
    state.sort = button.dataset.sort;
    renderFilters();
    renderList();
  });
  $("#filters").addEventListener("click", (event) => {
    const button = event.target.closest("button[data-filter]");
    if (!button) return;
    state.filter = button.dataset.filter;
    renderFilters();
    renderKpi();
    renderList();
  });

  $("#open-log").addEventListener("click", openLog);
  $("#scrim").addEventListener("click", closeDrawers);
  for (const button of document.querySelectorAll("[data-close]")) {
    button.addEventListener("click", closeDrawers);
  }
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeDrawers();
  });

  try {
    await loadProgram();
    if (state.pairs.length) {
      await loadCard(state.pairs[0].id);
    }
    renderAll();
    status(`обновлено ${new Date().toLocaleTimeString("ru-RU")}`, "ok");
  } catch (error) {
    status("данные не загрузились", "bad");
    showCardError(error);
  }

  connectEvents();
}

boot();
