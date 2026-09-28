-- 凝思 Studio 数据模型（SQLite）
-- 权威数据在库内；JSONL 台账（history/sessions.jsonl、scales/*.jsonl）作为审计副本由 paired_ledger 双写。

CREATE TABLE IF NOT EXISTS schema_version (
    version     TEXT PRIMARY KEY,
    applied_at  TEXT NOT NULL
);

-- 被试：只存匿名编号与粗粒度属性，姓名类直接身份信息不入库
CREATE TABLE IF NOT EXISTS subjects (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    public_id       TEXT NOT NULL UNIQUE,          -- sub-p01
    label           TEXT,                          -- 备注用别名（不含真实姓名要求见 README 隐私节）
    age_band        TEXT,
    sex             TEXT,
    handedness      TEXT,
    consent_version TEXT NOT NULL DEFAULT 'consent-v1',
    consent_at      TEXT,
    note            TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- 会话：一次完整实验流程
CREATE TABLE IF NOT EXISTS sessions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    uuid            TEXT NOT NULL UNIQUE,
    subject_id      INTEGER NOT NULL REFERENCES subjects(id) ON DELETE CASCADE,
    label           TEXT,
    device          TEXT,
    srate           REAL,
    channels        INTEGER,
    source          TEXT,                          -- sim-bsense / lsl:<name>
    time_scale      REAL NOT NULL DEFAULT 1.0,
    training_mode   TEXT NOT NULL DEFAULT 'quick',
    status          TEXT NOT NULL DEFAULT 'queued',-- queued|running|done|failed|cancelled
    phase           TEXT NOT NULL DEFAULT 'queued',
    progress        REAL NOT NULL DEFAULT 0.0,
    engine_versions TEXT NOT NULL DEFAULT '{}',
    error           TEXT,
    started_at      TEXT,
    ended_at        TEXT,
    created_at      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_subject ON sessions(subject_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_status ON sessions(status, created_at);

-- 阶段运行记录：每个阶段一行，带口径留痕与错误
CREATE TABLE IF NOT EXISTS runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    phase       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'running',   -- running|done|failed|skipped
    started_at  TEXT,
    ended_at    TEXT,
    duration_ms INTEGER,
    valid_ratio REAL,
    error       TEXT,
    payload     TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_runs_session ON runs(session_id, id);

-- 逐窗指标：kind = focus|relax|load|band_z|quality
CREATE TABLE IF NOT EXISTS metrics (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    name        TEXT NOT NULL,
    t_sec       REAL,
    value       REAL,
    mean        REAL,
    std         REAL,
    n           INTEGER,
    valid_ratio REAL,
    quality     TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_metrics_session ON metrics(session_id, kind, t_sec);

-- 预警事件
CREATE TABLE IF NOT EXISTS alerts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,
    state       TEXT NOT NULL,
    t_sec       REAL,
    value       REAL,
    sustained_sec REAL,
    message     TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_alerts_session ON alerts(session_id, t_sec);

-- 量表作答与计分
CREATE TABLE IF NOT EXISTS scale_runs (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id     INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    code           TEXT NOT NULL,
    version        TEXT,
    raw_score      INTEGER,
    standard_score INTEGER,
    level          TEXT,
    answered       INTEGER,
    missing        TEXT NOT NULL DEFAULT '[]',
    responses      TEXT NOT NULL DEFAULT '[]',
    items          TEXT NOT NULL DEFAULT '{}',
    created_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_scale_runs_session ON scale_runs(session_id, code);

-- 行为任务结果（SART / PVT-B）
CREATE TABLE IF NOT EXISTS behavior_runs (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    task        TEXT NOT NULL,
    spec        TEXT,
    metrics     TEXT NOT NULL DEFAULT '{}',
    trials      TEXT NOT NULL DEFAULT '{}',
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_behavior_runs_session ON behavior_runs(session_id, task);

-- 神经反馈训练分段
CREATE TABLE IF NOT EXISTS training_segments (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id       INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    seq              INTEGER NOT NULL,
    target           REAL,
    mean_score       REAL,
    on_target_ratio  REAL,
    hold_sec         REAL,
    excluded_windows INTEGER,
    samples          TEXT NOT NULL DEFAULT '[]',
    started_at       TEXT,
    ended_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_training_session ON training_segments(session_id, seq);

-- 产物文件
CREATE TABLE IF NOT EXISTS artifacts (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id  INTEGER NOT NULL REFERENCES sessions(id) ON DELETE CASCADE,
    kind        TEXT NOT NULL,                     -- report_md|report_json|heatmap_svg|trend_svg|model|zip|ledger
    path        TEXT NOT NULL,
    bytes       INTEGER,
    sha256      TEXT,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_artifacts_session ON artifacts(session_id, kind);
