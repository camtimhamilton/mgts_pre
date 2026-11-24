// Резервный канал: та же анкета, но отправляется прямо в приёмник.
// Нужна, если на площадке не работает внешняя форма или её интеграция.

const FACTORS = [
  ["f_motivation", {
    intern: "Насколько тебе сейчас интересно то, чем ты занимаешься?",
    mentor: "Насколько стажёр вовлечён в работу и проявляет инициативу?",
  }],
  ["f_clarity", {
    intern: "Насколько понятно, какие задачи и чего от тебя ждут?",
    mentor: "Насколько стажёр понимает свои задачи и ожидания?",
  }],
  ["f_mentor", {
    intern: "Насколько хватает поддержки и обратной связи от наставника?",
    mentor: "Насколько комфортно работать вместе и хватает ли времени на поддержку?",
  }],
  ["f_workload", {
    intern: "Насколько нагрузка сейчас комфортна?",
    mentor: "Насколько нагрузка стажёра адекватна?",
  }],
  ["f_inclusion", {
    intern: "Насколько ты чувствуешь себя частью команды?",
    mentor: "Насколько стажёр включён в жизнь команды?",
  }],
  ["f_future", {
    intern: "Насколько уверен, что хочешь и сможешь остаться в компании после стажировки?",
    mentor: "Насколько вы видите потенциал стажёра остаться в компании?",
  }],
];

const OPEN_BY_ROLE = {
  intern: {
    better: "Что за последний месяц изменилось к лучшему?",
    barrier: "Что мешает работать или снижает желание продолжать?",
  },
  mentor: {
    better: "Как изменилась самостоятельность стажёра за этот месяц?",
    barrier: "Что, на ваш взгляд, требует вмешательства?",
  },
};

const $ = (id) => document.getElementById(id);

function buildRanges() {
  const box = $("ranges");
  for (const [id, labels] of FACTORS) {
    const wrap = document.createElement("div");
    wrap.className = "field";

    const lab = document.createElement("label");
    lab.htmlFor = id;
    lab.dataset.factor = id;
    lab.textContent = labels.intern;

    const value = document.createElement("span");
    value.className = "range-value";
    value.textContent = "7";

    const input = document.createElement("input");
    input.type = "range";
    input.min = "1";
    input.max = "10";
    input.step = "1";
    input.value = "7";
    input.id = id;
    input.name = id;
    input.addEventListener("input", () => { value.textContent = input.value; });

    lab.append(value);
    wrap.append(lab, input);
    box.append(wrap);
  }
}

function applyRole(role) {
  const open = OPEN_BY_ROLE[role] || OPEN_BY_ROLE.intern;
  document.querySelector('label[for="q_better"]').textContent = open.better;
  document.querySelector('label[for="q_barrier"]').textContent = open.barrier;

  for (const [id, labels] of FACTORS) {
    const lab = document.querySelector(`label[data-factor="${id}"]`);
    if (!lab) continue;
    const value = lab.querySelector(".range-value");
    lab.textContent = labels[role] || labels.intern;
    lab.append(value);
  }
}

function payload(form) {
  const answers = {};
  for (const [id] of FACTORS) answers[id] = Number(form.elements[id].value);

  return {
    source: "selfhosted",
    role: form.elements.s_role.value,
    month: form.elements.s_month.value,
    s_name: form.elements.s_name.value.trim(),
    s_mentor: form.elements.s_mentor.value.trim(),
    s_direction: form.elements.s_direction.value.trim(),
    answers,
    open: {
      q_better: form.elements.q_better.value.trim(),
      q_barrier: form.elements.q_barrier.value.trim(),
    },
    submitted_at: new Date().toISOString(),
  };
}

async function submit(event) {
  event.preventDefault();
  const form = event.target;
  const result = $("result");
  const button = $("submit");

  if (!form.elements.s_name.value.trim()) {
    result.textContent = "Нужно имя стажёра — иначе ответ некуда привязать.";
    result.dataset.state = "bad";
    return;
  }

  button.disabled = true;
  result.textContent = "отправляю…";
  result.dataset.state = "";

  const headers = { "Content-Type": "application/json" };
  const key = form.elements.ingest_key.value.trim();
  if (key) headers["X-Ingest-Key"] = key;

  try {
    const res = await fetch("/api/ingest", {
      method: "POST",
      headers,
      body: JSON.stringify(payload(form)),
    });
    const data = await res.json();
    result.dataset.state = data.ok ? "ok" : "bad";
    result.textContent = data.duplicate
      ? `уже принимали: запись #${data.id}, дубликат отброшен`
      : `принято: запись #${data.id}, статус ${data.status}`;
  } catch (err) {
    result.dataset.state = "bad";
    result.textContent = `не ушло: ${err}`;
  } finally {
    button.disabled = false;
  }
}

buildRanges();
applyRole("intern");
$("s_role").addEventListener("change", (e) => applyRole(e.target.value));
$("survey").addEventListener("submit", submit);

fetch("/api/health")
  .then((r) => r.json())
  .then((d) => {
    $("team-name").textContent = d.team;
    $("foot-team").textContent = d.team;
  })
  .catch(() => {});
