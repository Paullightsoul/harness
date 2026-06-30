-- Состояние harness. Журнал events — источник правды для resume и наблюдаемости.
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS runs (
    id             TEXT PRIMARY KEY,
    project        TEXT NOT NULL,
    goal           TEXT NOT NULL,
    status         TEXT NOT NULL,
    base_branch    TEXT NOT NULL DEFAULT 'main',
    budget_credits REAL,
    spent_credits  REAL NOT NULL DEFAULT 0,
    created_at     TEXT NOT NULL,
    updated_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tasks (
    id            TEXT NOT NULL,
    run_id        TEXT NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
    title         TEXT NOT NULL,
    spec_path     TEXT NOT NULL,
    status        TEXT NOT NULL,
    depends_on    TEXT NOT NULL DEFAULT '[]',   -- JSON-массив id
    provides      TEXT NOT NULL DEFAULT '',
    complexity    TEXT NOT NULL DEFAULT 'normal', -- normal|high (high стартует с kimi)
    attempts      INTEGER NOT NULL DEFAULT 0,
    branch        TEXT NOT NULL DEFAULT '',
    worktree_path TEXT NOT NULL DEFAULT '',
    note          TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL,
    PRIMARY KEY (run_id, id)
);

CREATE TABLE IF NOT EXISTS attempts (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id        TEXT NOT NULL,
    task_id       TEXT NOT NULL,
    number        INTEGER NOT NULL,
    model         TEXT NOT NULL,
    worker_output TEXT NOT NULL DEFAULT '',
    gates_passed  INTEGER,                       -- 0/1/NULL
    verdict       TEXT,
    cost_credits  REAL NOT NULL DEFAULT 0,
    started_at    TEXT NOT NULL,
    finished_at   TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS reviews (
    attempt_id INTEGER NOT NULL,
    task_id    TEXT NOT NULL,
    verdict    TEXT NOT NULL,
    report     TEXT NOT NULL DEFAULT '',
    feedback   TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS events (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id  TEXT NOT NULL,
    type    TEXT NOT NULL,
    task_id TEXT,
    detail  TEXT NOT NULL DEFAULT '{}',
    at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_events_run ON events(run_id, id);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id, status);
CREATE INDEX IF NOT EXISTS idx_attempts_task ON attempts(run_id, task_id);
