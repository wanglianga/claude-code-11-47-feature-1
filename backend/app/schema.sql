-- ============================================================================
-- 防汛需求与道路情景覆盖数据分析平台 —— 数据库结构
-- 设计要点：
--   1. 来源目录中的每类原文（人口/节点/有向边/安置点/事件/边界）均带单位、
--      发生时间、登记时间、修订号与截止时间，可追溯到 source_documents。
--   2. 路网按“资料截止点”形成 graph_version；事件带生效区间与修订审计。
--   3. 情景工作区固定人口基准日、出发时刻、总时限、道路资料截止点。
--   4. 计算结果以完整情景签名入缓存索引，证据截止时间随结果保存。
-- ============================================================================

CREATE TABLE IF NOT EXISTS source_documents (
    doc_id              BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    dataset_type        TEXT NOT NULL CHECK (dataset_type IN
                        ('population','road_node','road_edge','shelter',
                         'road_event','boundary')),
    title               TEXT NOT NULL,
    source_org          TEXT,
    unit_note           TEXT,                     -- 原文计量单位说明
    source_occurred_at  TIMESTAMPTZ,              -- 原文所述事件发生时间
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    cutoff_at           TIMESTAMPTZ NOT NULL,     -- 该资料代表的证据截止时间
    revision_no         INTEGER NOT NULL DEFAULT 1,
    supersedes_doc_id   BIGINT REFERENCES source_documents(doc_id),
    raw_text            TEXT,                     -- 原文摘录
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 区域边界 --
CREATE TABLE IF NOT EXISTS region_boundaries (
    boundary_id         TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    geometry            JSONB NOT NULL,           -- [[x,y],...] 简单多边形
    revision_no         INTEGER NOT NULL DEFAULT 1,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    revised_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 道路节点 --
CREATE TABLE IF NOT EXISTS road_nodes (
    node_id             TEXT PRIMARY KEY,
    name                TEXT,
    geom_x              DOUBLE PRECISION NOT NULL,
    geom_y              DOUBLE PRECISION NOT NULL,
    allow_wait          BOOLEAN NOT NULL DEFAULT FALSE,  -- 是否允许在该节点等待
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    first_seen_at       TIMESTAMPTZ NOT NULL DEFAULT now(),
    updated_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ----------------------------------------------------- 有向边（版本化修订） --
-- 同一边的每次修订一行，用 valid_from/valid_to 表示有效期；截止点取数时
-- 只选 valid_from <= cutoff < valid_to 的修订。
CREATE TABLE IF NOT EXISTS road_edge_revisions (
    edge_id             TEXT NOT NULL,
    revision_no         INTEGER NOT NULL,
    from_node           TEXT NOT NULL REFERENCES road_nodes(node_id),
    to_node             TEXT NOT NULL REFERENCES road_nodes(node_id),
    length_m            DOUBLE PRECISION NOT NULL CHECK (length_m > 0),
    base_speed_kmh      DOUBLE PRECISION NOT NULL CHECK (base_speed_kmh > 0),
    valid_from          TIMESTAMPTZ NOT NULL,
    valid_to            TIMESTAMPTZ,                       -- NULL 表示至今有效
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    PRIMARY KEY (edge_id, revision_no),
    CHECK (valid_to IS NULL OR valid_to > valid_from)
);
CREATE INDEX IF NOT EXISTS idx_edge_rev_valid ON road_edge_revisions(valid_from, valid_to);

-- --------------------------------------------- 封路 / 分时限速事件（时变） --
-- 同一事件的每次修订各占一行（复合主键），快照按资料截止点取 received_at <= cutoff
-- 的最新修订，保证旧截止点分析在新修订录入后仍可精确复现。
CREATE TABLE IF NOT EXISTS road_event_registry (
    event_id            TEXT PRIMARY KEY,
    edge_id             TEXT NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN ('closure','speed_limit')),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS road_events (
    event_id            TEXT NOT NULL REFERENCES road_event_registry(event_id),
    revision_no         INTEGER NOT NULL,
    edge_id             TEXT NOT NULL,
    event_type          TEXT NOT NULL CHECK (event_type IN ('closure','speed_limit')),
    effective_from      TIMESTAMPTZ NOT NULL,
    effective_to        TIMESTAMPTZ,              -- NULL 表示尚未恢复
    speed_limit_kmh     DOUBLE PRECISION CHECK (speed_limit_kmh IS NULL OR speed_limit_kmh > 0),
    supersedes_event_id TEXT,
    status              TEXT NOT NULL DEFAULT 'active'
                        CHECK (status IN ('active','cancelled')),
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    received_at         TIMESTAMPTZ NOT NULL DEFAULT now(),  -- 平台收到该修订的时间
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (event_id, revision_no),
    CHECK (event_type = 'closure' OR speed_limit_kmh IS NOT NULL)
);
CREATE INDEX IF NOT EXISTS idx_event_edge ON road_events(edge_id);
CREATE INDEX IF NOT EXISTS idx_event_effective ON road_events(effective_from, effective_to);
CREATE INDEX IF NOT EXISTS idx_event_received ON road_events(received_at);

-- 事件修订审计：封路、延期、恢复（重开）、限速调整均留痕
CREATE TABLE IF NOT EXISTS road_event_revisions (
    audit_id            BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    event_id            TEXT NOT NULL REFERENCES road_event_registry(event_id),
    revision_no         INTEGER NOT NULL,
    change_summary      TEXT NOT NULL,
    change_json         JSONB NOT NULL,
    changed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    source_doc_id       BIGINT REFERENCES source_documents(doc_id)
);

-- ---------------------------------------------------------------- 安置点 --
CREATE TABLE IF NOT EXISTS shelters (
    shelter_id          TEXT PRIMARY KEY,
    name                TEXT NOT NULL,
    node_id             TEXT NOT NULL REFERENCES road_nodes(node_id),
    geom_x              DOUBLE PRECISION NOT NULL,
    geom_y              DOUBLE PRECISION NOT NULL,
    revision_no         INTEGER NOT NULL DEFAULT 1,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    revised_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    active              BOOLEAN NOT NULL DEFAULT TRUE
);

-- 安置点开放区间：到达时刻必须落在任一区间内才视为开放
CREATE TABLE IF NOT EXISTS shelter_open_windows (
    window_id           BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    shelter_id          TEXT NOT NULL REFERENCES shelters(shelter_id),
    open_from           TIMESTAMPTZ NOT NULL,
    open_to             TIMESTAMPTZ,                 -- NULL 表示长期开放
    revision_no         INTEGER NOT NULL DEFAULT 1,
    UNIQUE (shelter_id, open_from, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_shelter_window ON shelter_open_windows(shelter_id);

-- ---------------------------------------------------------------- 人口网格 --
CREATE TABLE IF NOT EXISTS pop_grids (
    grid_id             TEXT PRIMARY KEY,
    name                TEXT,
    geom_x              DOUBLE PRECISION NOT NULL,   -- 质心，供前端定位
    geom_y              DOUBLE PRECISION NOT NULL,
    boundary_id         TEXT REFERENCES region_boundaries(boundary_id),
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- 人口修订：同一网格按基准日取 as_of_date 当日可见的最新修订
CREATE TABLE IF NOT EXISTS pop_grid_revisions (
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    revision_no         INTEGER NOT NULL,
    population          DOUBLE PRECISION NOT NULL CHECK (population >= 0),
    as_of_date          DATE NOT NULL,               -- 人口基准日
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    changed_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (grid_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_pop_rev_asof ON pop_grid_revisions(as_of_date);

-- 网格→路网连接位置与互斥分配比例；多出口网格按子份额切分人口
CREATE TABLE IF NOT EXISTS grid_connectors (
    connector_id        BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    grid_id             TEXT NOT NULL REFERENCES pop_grids(grid_id),
    node_id             TEXT NOT NULL REFERENCES road_nodes(node_id),
    allocation_ratio    DOUBLE PRECISION NOT NULL CHECK
                        (allocation_ratio > 0 AND allocation_ratio <= 1),
    revision_no         INTEGER NOT NULL DEFAULT 1,
    source_doc_id       BIGINT REFERENCES source_documents(doc_id),
    UNIQUE (grid_id, node_id, revision_no)
);
CREATE INDEX IF NOT EXISTS idx_connector_grid ON grid_connectors(grid_id);

-- ------------------------------------------------- 路网版本（资料截止点） --
CREATE TABLE IF NOT EXISTS road_network_versions (
    version_id          BIGINT GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    cutoff_at           TIMESTAMPTZ NOT NULL,
    edge_fingerprint    TEXT NOT NULL,               -- 截止点边修订集合指纹
    event_fingerprint   TEXT NOT NULL,               -- 截止点事件集合指纹
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    UNIQUE (cutoff_at, edge_fingerprint, event_fingerprint)
);

-- ---------------------------------------------------------------- 情景工作区 --
CREATE TABLE IF NOT EXISTS scenarios (
    scenario_id         UUID PRIMARY KEY,
    name                TEXT NOT NULL,
    population_as_of    DATE NOT NULL,               -- 固定人口基准日
    departure_time      TIMESTAMPTZ NOT NULL,        -- 固定出发时刻
    max_trip_minutes    INTEGER NOT NULL CHECK (max_trip_minutes > 0),
    road_cutoff         TIMESTAMPTZ NOT NULL,        -- 道路资料截止点
    boundary_id         TEXT REFERENCES region_boundaries(boundary_id),  -- 分析边界，NULL=全部
    graph_version_id    BIGINT REFERENCES road_network_versions(version_id),
    signature           TEXT NOT NULL UNIQUE,        -- 完整输入的情景签名
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,        -- 随结果标注的证据截止时间
    created_by          TEXT NOT NULL DEFAULT 'analyst',
    note                TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 计算任务 --
-- ref_count 记录当前查看者数量；取消单个查看者只减引用，不归零不取消任务。
CREATE TABLE IF NOT EXISTS compute_tasks (
    task_id             UUID PRIMARY KEY,
    scenario_id         UUID NOT NULL REFERENCES scenarios(scenario_id),
    signature           TEXT NOT NULL,
    graph_version_id    BIGINT NOT NULL REFERENCES road_network_versions(version_id),
    kind                TEXT NOT NULL CHECK (kind IN ('precomputed','on_demand')),
    status              TEXT NOT NULL CHECK (status IN
                        ('queued','running','done','failed','cancelled'))
                        DEFAULT 'queued',
    ref_count           INTEGER NOT NULL DEFAULT 0,
    stale               BOOLEAN NOT NULL DEFAULT FALSE,  -- 路网更新后标记失效
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    error               TEXT,
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now(),
    started_at          TIMESTAMPTZ,
    finished_at         TIMESTAMPTZ
);
CREATE INDEX IF NOT EXISTS idx_task_signature ON compute_tasks(signature, status);

CREATE TABLE IF NOT EXISTS compute_task_viewers (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    viewer_id           TEXT NOT NULL,
    joined_at           TIMESTAMPTZ NOT NULL DEFAULT now(),
    PRIMARY KEY (task_id, viewer_id)
);

-- 任务实际经过的边依赖（来自见证路径），供封路修订精准定位受影响任务
CREATE TABLE IF NOT EXISTS task_edge_dependencies (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    edge_id             TEXT NOT NULL,
    PRIMARY KEY (task_id, edge_id)
);

-- 缓存索引：签名即完整输入，不同出发时刻/开放条件绝不共用缓存行
CREATE TABLE IF NOT EXISTS coverage_cache_index (
    signature           TEXT PRIMARY KEY,
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    graph_version_id    BIGINT NOT NULL,
    departure_time      TIMESTAMPTZ NOT NULL,
    max_trip_minutes    INTEGER NOT NULL,
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    stale               BOOLEAN NOT NULL DEFAULT FALSE,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 节点到达 --
CREATE TABLE IF NOT EXISTS node_arrivals (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    node_id             TEXT NOT NULL,
    arrival_time        TIMESTAMPTZ NOT NULL,
    wait_cost_sec       DOUBLE PRECISION NOT NULL DEFAULT 0,
    witness_node_path   JSONB NOT NULL,              -- 有序节点序列
    witness_edge_path   JSONB NOT NULL,              -- 有序边序列（见证路径）
    PRIMARY KEY (task_id, node_id)
);

-- ---------------------------------------------------------------- 网格份额结果 --
-- 每个互斥子份额一行；covered 去重在汇总阶段完成，同一网格只计一次。
CREATE TABLE IF NOT EXISTS grid_share_results (
    task_id             UUID NOT NULL REFERENCES compute_tasks(task_id),
    grid_id             TEXT NOT NULL,
    connector_node      TEXT NOT NULL,
    share_index         INTEGER NOT NULL,
    share_population    DOUBLE PRECISION NOT NULL,
    reachable           BOOLEAN NOT NULL,
    shelter_id          TEXT REFERENCES shelters(shelter_id),
    departure_time      TIMESTAMPTZ,
    arrival_time        TIMESTAMPTZ,
    total_wait_sec      DOUBLE PRECISION NOT NULL DEFAULT 0,
    witness_edge_path   JSONB,
    status              TEXT NOT NULL CHECK (status IN
                        ('covered','uncovered','unknown')),
    reason              TEXT,                        -- 被排除/未知原因
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    PRIMARY KEY (task_id, grid_id, share_index)
);
CREATE INDEX IF NOT EXISTS idx_share_task ON grid_share_results(task_id);

-- ---------------------------------------------------------------- 覆盖汇总 --
CREATE TABLE IF NOT EXISTS coverage_summaries (
    task_id             UUID PRIMARY KEY REFERENCES compute_tasks(task_id),
    total_population    DOUBLE PRECISION NOT NULL,   -- 边界内全部分母
    located_population  DOUBLE PRECISION NOT NULL,   -- 可定位到路网的人口
    unknown_population  DOUBLE PRECISION NOT NULL,   -- 无连接/缺分配依据
    covered_population  DOUBLE PRECISION NOT NULL,   -- 网格级去重后覆盖
    uncovered_population DOUBLE PRECISION NOT NULL,
    covered_grid_count  INTEGER NOT NULL,
    unknown_grid_count  INTEGER NOT NULL,
    computed_at         TIMESTAMPTZ NOT NULL DEFAULT now()
);

-- ---------------------------------------------------------------- 结果发布 --
-- input_manifest 保存本次比较采用的完整输入（修订号、截止点、窗口原文等）
CREATE TABLE IF NOT EXISTS publications (
    publication_id      UUID PRIMARY KEY,
    title               TEXT NOT NULL,
    scenario_a_id       UUID REFERENCES scenarios(scenario_id),
    scenario_b_id       UUID REFERENCES scenarios(scenario_id),
    input_manifest      JSONB NOT NULL,
    comparison_json     JSONB,
    evidence_cutoff_at  TIMESTAMPTZ NOT NULL,
    created_by          TEXT NOT NULL DEFAULT 'analyst',
    created_at          TIMESTAMPTZ NOT NULL DEFAULT now()
);
