// Страница импорта анкет: разбор файла → ручной маппинг колонок → загрузка.
// Контракт: docs/import-contract.md.
// Данные файла — чужие: любые строки в DOM попадают только через textContent.

const $ = (id) => document.getElementById(id);

const ALLOWED_EXT = [".xlsx", ".xlsm", ".csv"];

const FORMAT_LABELS = {
  long: "длинный формат",
  wide: "широкий формат",
  two_sheet: "книга с двумя листами",
  unknown: "формат не распознан",
};

const ROLE_LABELS = { intern: "стажёр", mentor: "наставник" };

// Поля, которые берутся из одной колонки файла.
const COLUMN_FIELDS = [
  ["intern_name", "Имя стажёра"],
  ["mentor_name", "Имя наставника"],
  ["direction", "Направление"],
  ["month", "Месяц"],
  ["role", "Роль (колонка)"],
];

const FACTOR_FIELDS = [
  ["motivation", "Мотивация"],
  ["clarity", "Ясность задач"],
  ["mentor", "Поддержка наставника"],
  ["workload", "Нагрузка"],
  ["inclusion", "Включённость в команду"],
  ["future", "Желание остаться"],
];

const OPEN_FIELDS = [
  ["q_better", "Что стало лучше"],
  ["q_barrier", "Что мешает"],
];

const state = {
  file: null,
  preview: null,
  busy: false,
};

// Ответы устаревших запросов не должны перерисовывать страницу.
let requestSeq = 0;

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function formatSize(bytes) {
  const size = Number(bytes);
  if (!Number.isFinite(size) || size < 0) return "—";
  if (size < 1024) return `${size} Б`;
  if (size < 1024 * 1024) {
    return `${(size / 1024).toFixed(size < 10240 ? 1 : 0)} КБ`;
  }
  return `${(size / (1024 * 1024)).toFixed(1)} МБ`;
}

function setStatus(text, tone) {
  const node = $("status-line");
  node.textContent = text || "";
  if (tone) node.dataset.state = tone;
  else delete node.dataset.state;
}

function setBusy(busy) {
  state.busy = Boolean(busy);
  $("preview-btn").disabled = state.busy || !state.file;
  $("commit-btn").disabled = state.busy || !state.preview;
}

function resetPreview() {
  requestSeq += 1;
  state.preview = null;

  $("preview-panel").hidden = true;
  $("commit-result").hidden = true;
  $("mapping").replaceChildren();
  $("sample-head").replaceChildren();
  $("sample-body").replaceChildren();
  $("warnings").replaceChildren();
  $("warnings").hidden = true;
  $("result-counters").replaceChildren();
  $("errors").replaceChildren();
  $("errors-wrap").hidden = true;
  $("errors-note").textContent = "";
  $("sample-note").textContent = "";

  const sheet = $("sheet-select");
  const auto = el("option", null, "— первый непустой —");
  auto.value = "";
  sheet.replaceChildren(auto);
  sheet.disabled = true;

  setBusy(false);
}

function setFile(file) {
  const name = file ? String(file.name || "") : "";
  const dot = name.lastIndexOf(".");
  const ext = dot >= 0 ? name.slice(dot).toLowerCase() : "";

  if (file && !ALLOWED_EXT.includes(ext)) {
    state.file = null;
    $("file-input").value = "";
    $("file-picked").textContent = "файл не выбран";
    resetPreview();
    setStatus(`Формат файла «${name}» не поддержан: нужен .xlsx, .xlsm или .csv.`, "bad");
    return;
  }

  state.file = file || null;
  $("file-picked").textContent = file
    ? `${name} · ${formatSize(file.size)}`
    : "файл не выбран";
  resetPreview();
  setStatus(file ? "Файл выбран. Нажми «Разобрать», чтобы увидеть колонки." : "", "");
}

function buildPreviewForm() {
  const form = new FormData();
  form.append("file", state.file, state.file.name);

  const sheet = $("sheet-select").value;
  if (sheet) form.append("sheet", sheet);

  const role = $("default-role").value;
  if (role) form.append("default_role", role);

  return form;
}

function collectMapping() {
  const column = (id) => {
    const node = $(id);
    return node && node.value ? node.value : null;
  };

  const mapping = {
    intern_name: column("map-intern_name"),
    mentor_name: column("map-mentor_name"),
    direction: column("map-direction"),
    month: column("map-month"),
    role: column("map-role"),
    default_role: $("default-role").value || null,
    factors: {},
    open: {},
  };

  for (const [key] of FACTOR_FIELDS) {
    mapping.factors[key] = column(`map-factor-${key}`);
  }
  for (const [key] of OPEN_FIELDS) {
    mapping.open[key] = column(`map-open-${key}`);
  }

  return mapping;
}

async function readJson(res) {
  try {
    return await res.json();
  } catch (_err) {
    return null;
  }
}

function detailText(data) {
  if (!data || data.detail === undefined || data.detail === null) return "";
  const detail = data.detail;
  if (typeof detail === "string") return detail;
  if (Array.isArray(detail)) {
    return detail
      .map((item) => (item && typeof item.msg === "string" ? item.msg : String(item)))
      .join("; ");
  }
  return String(detail);
}

function errorText(status, data) {
  const detail = detailText(data);

  if (status === 501) {
    return "Импорт ещё не включён на бэкенде: сервер отвечает 501. Страница готова — попробуй позже, когда импорт дореализуют.";
  }
  if (status === 415) {
    return "Формат файла не поддержан: нужен .xlsx, .xlsm или .csv.";
  }
  if (status === 422) {
    return detail
      ? `Файл не читается: ${detail}`
      : "Файл не читается или пуст.";
  }
  if (status === 404) {
    return "Маршрут импорта не найден (404). Похоже, бэкенд ещё не подключил эти эндпоинты.";
  }
  return detail
    ? `Сервер ответил ошибкой ${status}: ${detail}`
    : `Сервер ответил ошибкой ${status}. Попробуй ещё раз.`;
}

function networkErrorText(err) {
  const note = err && err.message ? err.message : "нет связи";
  return `Не удалось связаться с сервером (${note}). Проверь, что API запущен, и повтори.`;
}

function renderConfidence(value) {
  const num = Number(value);
  const known = Number.isFinite(num);
  const percent = known ? Math.max(0, Math.min(100, Math.round(num * 100))) : 0;
  $("confidence-value").textContent = known ? `${percent}%` : "—";
  $("confidence-fill").style.width = `${percent}%`;
}

function renderWarnings(warnings) {
  const list = $("warnings");
  list.replaceChildren();

  const items = Array.isArray(warnings)
    ? warnings.filter((item) => typeof item === "string" && item.trim())
    : [];

  if (!items.length) {
    list.hidden = true;
    return;
  }
  for (const item of items) list.append(el("li", null, item));
  list.hidden = false;
}

function renderSheets(file) {
  const select = $("sheet-select");
  const sheets = Array.isArray(file.sheets) ? file.sheets.map(String) : [];
  const current = select.value;

  const auto = el("option", null, "— первый непустой —");
  auto.value = "";
  select.replaceChildren(auto);

  for (const name of sheets) {
    const option = el("option", null, name);
    option.value = name;
    select.append(option);
  }

  if (sheets.includes(current)) select.value = current;
  select.disabled = sheets.length === 0;
}

function renderSample(data) {
  const columns = Array.isArray(data.columns) ? data.columns.map(String) : [];
  const head = $("sample-head");
  const body = $("sample-body");
  head.replaceChildren();
  body.replaceChildren();

  for (const column of columns) head.append(el("th", null, column));

  const rows = Array.isArray(data.sample) ? data.sample.slice(0, 5) : [];
  if (!rows.length) {
    const tr = el("tr");
    const td = el("td", "muted", "В примере нет строк.");
    td.colSpan = Math.max(1, columns.length);
    tr.append(td);
    body.append(tr);
  } else {
    for (const item of rows) {
      const tr = el("tr");
      for (const column of columns) {
        const value = item && typeof item === "object" ? item[column] : undefined;
        const empty = value === undefined || value === null || value === "";
        tr.append(el("td", empty ? "cell-empty" : null, empty ? "—" : String(value)));
      }
      body.append(tr);
    }
  }

  const total = data.rows_total !== undefined && data.rows_total !== null
    ? String(data.rows_total)
    : "—";
  $("sample-note").textContent = rows.length
    ? `показаны первые ${rows.length} из ${total} строк`
    : `строк в файле: ${total}`;
}

function makeColumnField(id, labelText, mapped, columns) {
  const field = el("div", "field");

  const label = el("label", null, labelText);
  label.htmlFor = id;

  const select = el("select");
  select.id = id;

  const none = el("option", null, "— нет —");
  none.value = "";
  select.append(none);

  for (const column of columns) {
    const option = el("option", null, column);
    option.value = column;
    select.append(option);
  }

  const value = mapped === null || mapped === undefined ? "" : String(mapped);
  if (value) {
    if (!columns.includes(value)) {
      const extra = el("option", null, value);
      extra.value = value;
      select.append(extra);
    }
    select.value = value;
  }

  field.append(label, select);
  return field;
}

function makeDefaultRoleField() {
  const panel = $("default-role");
  const field = el("div", "field");

  const label = el("label", null, "Роль по умолчанию");
  label.htmlFor = "map-default-role";

  const select = el("select");
  select.id = "map-default-role";
  for (const [value, text] of [
    ["", "— не задана —"],
    ["intern", "Стажёр"],
    ["mentor", "Наставник"],
  ]) {
    const option = el("option", null, text);
    option.value = value;
    select.append(option);
  }
  select.value = panel.value;
  select.addEventListener("change", () => {
    panel.value = select.value;
    updateRoleHint();
  });

  field.append(label, select);
  return field;
}

function updateRoleHint() {
  const hint = $("map-role-hint");
  if (!hint) return;
  const role = $("default-role").value;
  hint.textContent = role
    ? `Если колонки роли нет, все строки получат роль «${ROLE_LABELS[role] || role}».`
    : "Если колонки роли нет, используется «Роль по умолчанию».";
}

function renderMapping(data) {
  const box = $("mapping");
  box.replaceChildren();

  const columns = Array.isArray(data.columns) ? data.columns.map(String) : [];
  const mapping = data.mapping && typeof data.mapping === "object" ? data.mapping : {};
  const factors = mapping.factors && typeof mapping.factors === "object" ? mapping.factors : {};
  const open = mapping.open && typeof mapping.open === "object" ? mapping.open : {};

  const defaultRole = $("default-role");
  if (!defaultRole.value && (mapping.default_role === "intern" || mapping.default_role === "mentor")) {
    defaultRole.value = mapping.default_role;
  }

  const main = el("div", "map-section");
  main.append(el("h3", null, "Основные поля"));
  const mainGrid = el("div", "map-grid");
  for (const [key, label] of COLUMN_FIELDS) {
    mainGrid.append(makeColumnField(`map-${key}`, label, mapping[key], columns));
  }
  mainGrid.append(makeDefaultRoleField());
  main.append(mainGrid);

  const roleHint = el("p", "hint");
  roleHint.id = "map-role-hint";
  main.append(roleHint);
  box.append(main);
  updateRoleHint();

  const factorSection = el("div", "map-section");
  factorSection.append(el("h3", null, "Оценки: шесть факторов"));
  const factorGrid = el("div", "map-grid");
  for (const [key, label] of FACTOR_FIELDS) {
    factorGrid.append(makeColumnField(`map-factor-${key}`, label, factors[key], columns));
  }
  factorSection.append(factorGrid);
  box.append(factorSection);

  const openSection = el("div", "map-section");
  openSection.append(el("h3", null, "Открытые ответы"));
  const openGrid = el("div", "map-grid");
  for (const [key, label] of OPEN_FIELDS) {
    openGrid.append(makeColumnField(`map-open-${key}`, label, open[key], columns));
  }
  openSection.append(openGrid);
  box.append(openSection);
}

function renderPreview(data) {
  state.preview = data;
  $("preview-panel").hidden = false;
  $("commit-result").hidden = true;

  const file = data.file && typeof data.file === "object" ? data.file : {};
  $("file-name").textContent = file.name
    ? String(file.name)
    : state.file
      ? state.file.name
      : "—";
  $("file-size").textContent = file.size !== undefined && file.size !== null
    ? formatSize(file.size)
    : "—";
  $("file-sheets").textContent = Array.isArray(file.sheets) && file.sheets.length
    ? file.sheets.map(String).join(", ")
    : "—";
  $("rows-total").textContent = data.rows_total !== undefined && data.rows_total !== null
    ? String(data.rows_total)
    : "—";

  const format = typeof data.format === "string" ? data.format : "unknown";
  $("format-badge").textContent = FORMAT_LABELS[format] || format;

  renderConfidence(data.confidence);
  renderWarnings(data.warnings);
  renderSheets(file);
  renderSample(data);
  renderMapping(data);
  setBusy(false);
}

function numberText(value) {
  const num = Number(value);
  return Number.isFinite(num) ? String(num) : "—";
}

function renderCommit(data) {
  const counters = [
    ["Создано", data.created, "ok"],
    ["Обновлено", data.updated, ""],
    ["Дубликатов", data.duplicates, "warn"],
    ["Несопоставлено", data.unmatched, "warn"],
    ["Ошибок", data.errors_total, "bad"],
  ];

  const box = $("result-counters");
  box.replaceChildren();
  for (const [label, value, tone] of counters) {
    const card = el("div", "counter");
    if (tone) card.dataset.tone = tone;
    card.append(
      el("span", "counter-value", numberText(value)),
      el("span", "counter-label", label),
    );
    box.append(card);
  }

  const errors = Array.isArray(data.errors) ? data.errors.slice(0, 20) : [];
  const body = $("errors");
  body.replaceChildren();
  for (const item of errors) {
    const row = el("tr");
    const rowNumber = item && item.row !== undefined && item.row !== null
      ? String(item.row)
      : "—";
    const message = item && typeof item.error === "string"
      ? item.error
      : typeof item === "string"
        ? item
        : "неизвестная ошибка";
    row.append(el("td", null, rowNumber), el("td", null, message));
    body.append(row);
  }

  const total = Number(data.errors_total);
  const shown = errors.length;
  $("errors-wrap").hidden = shown === 0;
  $("errors-note").textContent = shown
    ? Number.isFinite(total) && total > shown
      ? `показаны первые ${shown} из ${total}`
      : `всего: ${Number.isFinite(total) ? total : shown}`
    : "";
  $("commit-result").hidden = false;
}

async function runPreview() {
  if (state.busy || !state.file) return;

  const token = ++requestSeq;
  setBusy(true);
  setStatus("Разбираю файл…", "");

  try {
    const res = await fetch("/api/import/preview", {
      method: "POST",
      body: buildPreviewForm(),
    });
    const data = await readJson(res);
    if (token !== requestSeq) return;

    if (!res.ok) {
      setStatus(errorText(res.status, data), "bad");
      return;
    }
    if (!data || typeof data !== "object") {
      setStatus("Сервер вернул неожиданный ответ на разбор. Попробуй ещё раз.", "bad");
      return;
    }

    renderPreview(data);
    setStatus("Разбор готов. Проверь маппинг и нажми «Загрузить».", "ok");
  } catch (err) {
    if (token !== requestSeq) return;
    setStatus(networkErrorText(err), "bad");
  } finally {
    if (token === requestSeq) setBusy(false);
  }
}

async function runCommit() {
  if (state.busy || !state.preview || !state.file) return;

  const token = ++requestSeq;
  setBusy(true);
  setStatus("Загружаю файл…", "");

  try {
    const form = buildPreviewForm();
    form.append("mapping", JSON.stringify(collectMapping()));

    const res = await fetch("/api/import/commit", {
      method: "POST",
      body: form,
    });
    const data = await readJson(res);
    if (token !== requestSeq) return;

    if (!res.ok) {
      setStatus(errorText(res.status, data), "bad");
      return;
    }
    if (!data || typeof data !== "object") {
      setStatus("Сервер вернул неожиданный ответ на загрузку. Загляни в лог приёмника.", "bad");
      return;
    }

    renderCommit(data);
    setStatus("Готово: результат загрузки ниже.", "ok");
  } catch (err) {
    if (token !== requestSeq) return;
    setStatus(networkErrorText(err), "bad");
  } finally {
    if (token === requestSeq) setBusy(false);
  }
}

function onSheetChange() {
  if (!state.preview) return;
  // Маппинг собран под другой лист — до нового разбора грузить нельзя.
  state.preview = null;
  $("commit-result").hidden = true;
  setBusy(false);
  setStatus("Лист изменён. Нажми «Разобрать», чтобы пересобрать разбор.", "");
}

const dropzone = $("dropzone");
const fileInput = $("file-input");

fileInput.addEventListener("change", () => {
  const file = fileInput.files && fileInput.files[0];
  if (file) setFile(file);
});

for (const type of ["dragenter", "dragover"]) {
  dropzone.addEventListener(type, (event) => {
    event.preventDefault();
    dropzone.dataset.drag = "true";
  });
}

for (const type of ["dragleave", "dragend"]) {
  dropzone.addEventListener(type, () => {
    delete dropzone.dataset.drag;
  });
}

dropzone.addEventListener("drop", (event) => {
  event.preventDefault();
  delete dropzone.dataset.drag;
  const transfer = event.dataTransfer;
  const file = transfer && transfer.files && transfer.files[0];
  if (file) setFile(file);
});

dropzone.addEventListener("keydown", (event) => {
  if (event.key === "Enter" || event.key === " ") {
    event.preventDefault();
    fileInput.click();
  }
});

$("preview-btn").addEventListener("click", runPreview);
$("commit-btn").addEventListener("click", runCommit);
$("sheet-select").addEventListener("change", onSheetChange);
$("default-role").addEventListener("change", () => {
  const mappingRole = $("map-default-role");
  if (mappingRole) mappingRole.value = $("default-role").value;
  updateRoleHint();
});

// Файл, брошенный мимо зоны, не должен открываться вместо страницы.
window.addEventListener("dragover", (event) => event.preventDefault());
window.addEventListener("drop", (event) => event.preventDefault());

setBusy(false);

fetch("/api/health")
  .then((res) => res.json())
  .then((data) => {
    if (data && data.team) {
      $("team-name").textContent = data.team;
      $("foot-team").textContent = data.team;
    }
  })
  .catch(() => {});
