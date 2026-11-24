PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- Пара «стажёр — наставник». Единица наблюдения в дашборде.
CREATE TABLE IF NOT EXISTS pairs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    intern_name TEXT    NOT NULL,
    mentor_name TEXT,
    direction   TEXT,
    start_month TEXT,
    status      TEXT    NOT NULL DEFAULT 'active',
    photo_url   TEXT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Одна анкета. Ключ идемпотентности: (pair_id, role, month).
CREATE TABLE IF NOT EXISTS submissions (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id      INTEGER REFERENCES pairs(id) ON DELETE CASCADE,
    role         TEXT    NOT NULL CHECK (role IN ('intern', 'mentor')),
    month        TEXT    NOT NULL,
    source       TEXT    NOT NULL,
    raw_text     TEXT,
    payload_json TEXT    NOT NULL,
    external_id  TEXT,
    received_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    content_hash TEXT    NOT NULL,
    status       TEXT    NOT NULL DEFAULT 'new'
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_submissions_pair_role_month
    ON submissions (pair_id, role, month);

-- Результат разбора одной анкеты моделью.
CREATE TABLE IF NOT EXISTS analyses (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    submission_id INTEGER NOT NULL REFERENCES submissions(id) ON DELETE CASCADE,
    model         TEXT,
    prompt_version TEXT,
    result_json   TEXT,
    created_at    TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Иммутабельный снимок расчёта на пару и месяц.
CREATE TABLE IF NOT EXISTS snapshots (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id      INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    month        TEXT    NOT NULL,
    risk         INTEGER NOT NULL,
    base         REAL    NOT NULL,
    gap_penalty  REAL    NOT NULL DEFAULT 0,
    text_penalty REAL    NOT NULL DEFAULT 0,
    delta        INTEGER,
    band         TEXT    NOT NULL,
    data_json    TEXT    NOT NULL,
    created_at   TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_snapshots_pair_month
    ON snapshots (pair_id, month);

CREATE TABLE IF NOT EXISTS factors (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id     INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    key             TEXT    NOT NULL,
    points          REAL    NOT NULL,
    score_intern    REAL,
    score_mentor    REAL,
    score_origin    TEXT    NOT NULL DEFAULT 'answer',
    quote           TEXT
);

CREATE TABLE IF NOT EXISTS steps (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    snapshot_id INTEGER NOT NULL REFERENCES snapshots(id) ON DELETE CASCADE,
    key         TEXT    NOT NULL,
    text        TEXT    NOT NULL,
    reduction   REAL    NOT NULL,
    owner       TEXT,
    due         TEXT
);

CREATE TABLE IF NOT EXISTS outcomes (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    pair_id   INTEGER NOT NULL REFERENCES pairs(id) ON DELETE CASCADE,
    result    TEXT    NOT NULL CHECK (result IN ('stayed', 'left', 'finished')),
    marked_at TEXT    NOT NULL DEFAULT (datetime('now'))
);

-- Сырые входящие запросы. Главный инструмент отладки на финале.
CREATE TABLE IF NOT EXISTS ingest_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    source       TEXT    NOT NULL,
    delivery_id  TEXT,
    status       TEXT    NOT NULL,
    raw          TEXT,
    headers_json TEXT,
    error        TEXT,
    received_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_ingest_delivery
    ON ingest_log (delivery_id);

CREATE INDEX IF NOT EXISTS ix_ingest_received
    ON ingest_log (received_at DESC);
