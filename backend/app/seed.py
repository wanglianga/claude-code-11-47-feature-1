"""样例数据种子。

小型样例（small）可手工复算：所有边长 1000m、基础速度 60km/h，即每条边 1 分钟。
基准时刻 T0 = 2026-07-15 08:00 UTC。

路网拓扑（含单向边与绕行）：
    N1 → N2 → N3 → N4 → N7        E3(N3→N4) 08:05-08:20 临时封闭，N3 不可等待
          ↓        ↑               E7(N4→N7) 08:00-08:40 封闭，08:40 重开（恢复验证）
          N5 → N6 ─┘               E1(N1→N2) 08:10-08:30 分时限速 20km/h（3 分钟）
    （N1→N2 为单向边，不存在反向边）
安置点：S1@N3（07:00-18:00）、S2@N6（07:00-18:00）、S3@N7（仅 09:00-12:00 开放）
N4 只有 E7 一个出口，故 G8@N4 能否覆盖完全取决于 E7 重开与 S3 开放：
C1 截止点 EV3 为“尚未恢复”的封路 → G8 不可覆盖；C2 修订重开 → G8 恢复覆盖。

情形覆盖：
  - 单向边 E1、绕行 N2→N5→N6→N4、分时限速 EV2、中途封闭 EV1/EV3；
  - G1 单份额可同时到 S2/S3 之外的两个点（多点重叠，只计一次）；
  - G2 双出口互斥份额 0.6/0.4；
  - G3 无连接、G4 比例缺口、G5 比例>1 → 未知口径；
  - G6 两次人口修订 → 共同分母对比中的人口资料变化；
  - G7 属于另一边界 → 分析边界变化；
  - 截止点 C2 录入 E7 重开与新封路 → 缓存失效与恢复重算。

连接证据治理样例（GR 系列，清河 RIVER1 沿 y=-600 横贯）：
  - GR1 河岸村片：低精度村级质心候选跨河指向最近节点 N1（自动排除，
    禁止按最近直线距离默认接入），另有地址标准化候选接入同侧 N9 → 确定分类；
  - GR2 南堰村片：仅低精度村级质心候选 → 整格连接不确定，人工确认后转确定；
  - GR3 误标片：地址标准化误接到孤屯 N8（确定不可达），人工纠正接入 N3 后
    形成新连接版本，旧情景与发布不被改写；
  - GR4 双候选片：最近直线距离是断头路 N11，主干路候选在 N6，两者均未配
    互斥权重 → 整格连接不确定，上界含 N6 可达性，人口只计一次；
  - GR5 东站引道片：弱证据候选接 N4，C1 时 E7 未恢复 → 不确定且上界为 0；
    C2 重开且 60 分钟时限 → 上界包含该片人口（道路修复后的重新计算）。
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from psycopg.types.json import Json

UTC = timezone.utc
T0 = datetime(2026, 7, 15, 8, 0, tzinfo=UTC)
C1 = datetime(2026, 7, 15, 7, 30, tzinfo=UTC)   # 第一道路资料截止点
C2 = datetime(2026, 7, 15, 8, 45, tzinfo=UTC)   # 第二截止点（含临时事件修订）


def _dt(minutes: float) -> datetime:
    return T0 + timedelta(minutes=minutes)


def _add_doc(cur, dataset_type, title, unit_note, occurred, cutoff, revision=1,
             supersedes=None, raw="", org="市防汛办"):
    cur.execute(
        """INSERT INTO source_documents
               (dataset_type, title, source_org, unit_note, source_occurred_at,
                received_at, cutoff_at, revision_no, supersedes_doc_id, raw_text)
           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING doc_id""",
        (dataset_type, title, org, unit_note, occurred, cutoff - timedelta(minutes=5),
         cutoff, revision, supersedes, raw),
    )
    return cur.fetchone()["doc_id"]


def reseed(conn) -> None:
    """清空并重建全部样例（幂等）。"""
    with conn.cursor() as cur:
        cur.execute(
            """
            TRUNCATE TABLE
                grid_candidate_results, grid_share_results, node_arrivals,
                coverage_summaries, coverage_cache_index, compute_task_viewers,
                task_edge_dependencies, compute_tasks, publications, scenarios,
                connection_reviews, grid_connection_candidates, connection_versions,
                spatial_barriers,
                pop_grid_revisions, pop_grids, shelter_open_windows, shelters,
                road_event_revisions, road_events, road_event_registry,
                road_edge_revisions, road_nodes,
                region_boundaries, road_network_versions, source_documents
            RESTART IDENTITY CASCADE
            """
        )
    _seed_boundaries(conn)
    _seed_documents_and_network(conn)
    _seed_shelters(conn)
    _seed_population(conn)
    _seed_connection_evidence(conn)
    conn.commit()


def _seed_boundaries(conn) -> None:
    doc = None
    with conn.cursor() as cur:
        doc = _add_doc(cur, "boundary", "行政区划边界（2026 版）", "坐标单位：米（地方平面坐标系）",
                       datetime(2026, 1, 1, tzinfo=UTC), C1,
                       raw="POLYGON 边界原文摘录：主城区 B_MAIN 与扩展区 B_EXT")
        cur.execute(
            "INSERT INTO region_boundaries (boundary_id, name, geometry, revision_no, source_doc_id) "
            "VALUES (%s,%s,%s,1,%s)",
            ("B_MAIN", "主城区", Json([[-1500, -1200], [4500, -1200], [4500, 2000], [-1500, 2000]]), doc),
        )
        cur.execute(
            "INSERT INTO region_boundaries (boundary_id, name, geometry, revision_no, source_doc_id) "
            "VALUES (%s,%s,%s,1,%s)",
            ("B_EXT", "扩展区", Json([[4500, -1200], [6500, -1200], [6500, 2000], [4500, 2000]]), doc),
        )


def _seed_documents_and_network(conn) -> None:
    with conn.cursor() as cur:
        node_doc = _add_doc(cur, "road_node", "路网节点台账", "坐标单位：米",
                            datetime(2026, 6, 1, tzinfo=UTC), C1,
                            raw="节点 N1..N8 原文清单，含是否允许路边等待标记")
        edge_doc = _add_doc(cur, "road_edge", "路网有向边台账", "长度：米；速度：km/h",
                            datetime(2026, 6, 1, tzinfo=UTC), C1,
                            raw="有向边原文：单向通行路段需标注方向，限速为常规晴好天气限速")

        nodes = [
            ("N1", "城北路口", 0, 0, True),
            ("N2", "中心市场", 1000, 0, True),
            ("N3", "河东桥头", 2000, 0, False),
            ("N4", "体育中心", 3000, 0, True),
            ("N5", "西环绕道", 1000, 1000, False),
            ("N6", "南门广场", 2000, 1000, True),
            ("N7", "火车东站", 4000, 0, True),
            ("N8", "孤屯", -1000, 0, False),
            ("N9", "南岸村口", 1200, -1000, False),
            ("N10", "南堰村口", 1500, -800, False),
            ("N11", "港尾路尾", 2300, 1100, False),
        ]
        cur.executemany(
            "INSERT INTO road_nodes (node_id, name, geom_x, geom_y, allow_wait, source_doc_id) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            [(nid, name, x, y, wait, node_doc) for nid, name, x, y, wait in nodes],
        )

        # (edge_id, from, to)；除标注外全部 1000m / 60kmh → 1 分钟，边方向即通行方向
        edges = [
            ("E1", "N1", "N2", 1000),   # 单向边：无 E1 反向
            ("E2", "N2", "N3", 1000),
            ("E3", "N3", "N4", 1000),
            ("E4", "N2", "N5", 1000),
            ("E5", "N5", "N6", 1000),
            ("E6", "N6", "N4", 1000),
            ("E7", "N4", "N7", 1000),
            ("E8", "N6", "N3", 1000),
            ("E9", "N9", "N2", 1020),   # 南岸村跨河桥（车行）
            ("E10", "N10", "N2", 940),  # 南堰村跨河桥（车行）
            ("E11", "N6", "N11", 300),  # 港尾路：驶入断头路，N11 无出边
        ]
        cur.executemany(
            """INSERT INTO road_edge_revisions
                   (edge_id, revision_no, from_node, to_node, length_m, base_speed_kmh,
                    valid_from, source_doc_id)
               VALUES (%s,1,%s,%s,%s,60,%s,%s)""",
            [(eid, f, t, length, datetime(2026, 6, 1, tzinfo=UTC), edge_doc)
             for eid, f, t, length in edges],
        )

        # 空间阻隔：清河沿 y=-600 横贯（候选接入的跨河判定依据）
        barrier_doc = _add_doc(
            cur, "barrier", "水系与行洪阻隔图册", "坐标单位：米（折线）",
            datetime(2026, 5, 20, tzinfo=UTC), C1,
            raw="清河 RIVER1：沿 y=-600 横贯主城区，仅桥梁处可通行",
        )
        cur.execute(
            "INSERT INTO spatial_barriers (barrier_id, name, geometry, source_doc_id) "
            "VALUES (%s,%s,%s,%s)",
            ("RIVER1", "清河", Json([[-1500, -600], [4500, -600]]), barrier_doc),
        )

        ev_doc1 = _add_doc(
            cur, "road_event", "暴雨交通管制通告（第 1 号）", "时间：本地时刻(UTC)；速度：km/h",
            T0, C1,
            raw="E3 河东桥 08:05-08:20 封闭；E7 东站引道 08:00 起封闭、恢复时间待定；E1 分时限速 20km/h 08:10-08:30",
        )
        events = [
            # event_id, type, edge, from, to, speed
            ("EV1", "closure", "E3", _dt(5), _dt(20), None),
            ("EV2", "speed_limit", "E1", _dt(10), _dt(30), 20.0),
            # C1 时 E7 封路尚未恢复（effective_to 为空）；C2 以 *新修订行* 记录 08:40 重开
            ("EV3", "closure", "E7", _dt(0), None, None),
        ]
        cur.executemany(
            "INSERT INTO road_event_registry (event_id, edge_id, event_type) VALUES (%s,%s,%s)",
            [(eid, edge, typ) for eid, typ, edge, ef, et, spd in events],
        )
        cur.executemany(
            """INSERT INTO road_events
                   (event_id, revision_no, event_type, edge_id, effective_from, effective_to,
                    speed_limit_kmh, supersedes_event_id, status, source_doc_id, received_at)
               VALUES (%s,1,%s,%s,%s,%s,%s,NULL,'active',%s,%s)""",
            [(eid, typ, edge, ef, et, spd, ev_doc1, C1 - timedelta(minutes=10))
             for eid, typ, edge, ef, et, spd in events],
        )
        cur.executemany(
            """INSERT INTO road_event_revisions
                   (event_id, revision_no, change_summary, change_json, source_doc_id)
               VALUES (%s,1,%s,%s,%s)""",
            [
                ("EV1", "首发：河东桥临时封闭", Json({"effective_from": "08:05", "effective_to": "08:20"}), ev_doc1),
                ("EV2", "首发：分时限速 20km/h", Json({"speed_limit_kmh": 20}), ev_doc1),
                ("EV3", "首发：东站引道封闭（恢复时间待定）", Json({"effective_from": "08:00", "effective_to": None}), ev_doc1),
            ],
        )


def _seed_shelters(conn) -> None:
    with conn.cursor() as cur:
        doc = _add_doc(cur, "shelter", "既有应急安置点名录与开放安排", "容量单位：人（本平台不做分配）",
                       datetime(2026, 7, 1, tzinfo=UTC), C1,
                       raw="S1 河东桥旁社区馆（N3）、S2 南门学校（N6）、S3 东站展厅（N7，仅上午开放）")
        shelters = [
            ("S1", "河东桥社区安置点", "N3", 2000, 0),
            ("S2", "南门学校安置点", "N6", 2000, 1000),
            ("S3", "东站展厅安置点", "N7", 4000, 0),
        ]
        cur.executemany(
            "INSERT INTO shelters (shelter_id, name, node_id, geom_x, geom_y, revision_no, source_doc_id) "
            "VALUES (%s,%s,%s,%s,%s,1,%s)",
            [(sid, name, node, x, y, doc) for sid, name, node, x, y in shelters],
        )
        windows = [
            ("S1", datetime(2026, 7, 15, 7, tzinfo=UTC), datetime(2026, 7, 15, 18, tzinfo=UTC)),
            ("S2", datetime(2026, 7, 15, 7, tzinfo=UTC), datetime(2026, 7, 15, 18, tzinfo=UTC)),
            ("S3", datetime(2026, 7, 15, 9, tzinfo=UTC), datetime(2026, 7, 15, 12, tzinfo=UTC)),
        ]
        cur.executemany(
            "INSERT INTO shelter_open_windows (shelter_id, open_from, open_to, revision_no) "
            "VALUES (%s,%s,%s,1)",
            windows,
        )


def _seed_population(conn) -> None:
    with conn.cursor() as cur:
        doc_v1 = _add_doc(
            cur, "population", "规则网格常住人口（6 月基准）", "单位：人；网格 500m×500m",
            datetime(2026, 6, 1, tzinfo=UTC),
            datetime(2026, 6, 30, tzinfo=UTC), revision=1,
            raw="规则网格人口原文表 v1：G1..G7",
        )
        doc_v2 = _add_doc(
            cur, "population", "规则网格常住人口（7 月修订）", "单位：人；网格 500m×500m",
            datetime(2026, 7, 1, tzinfo=UTC),
            datetime(2026, 7, 14, tzinfo=UTC), revision=2, supersedes=doc_v1,
            raw="修订 v2：G6 新交付小区入住，人口 1000 → 1200",
        )

        # grid_id, name, x, y, boundary, pop_v1, pop_v2(None=不修订)
        grids = [
            ("G1", "网格 G1（南门重叠片）", 2000, 800, "B_MAIN", 1000, None),
            ("G2", "网格 G2（双出口片）", 1000, 500, "B_MAIN", 500, None),
            ("G3", "网格 G3（无连接片）", -1000, 800, "B_MAIN", 300, None),
            ("G4", "网格 G4（分配缺口片）", 200, 200, "B_MAIN", 800, None),
            ("G5", "网格 G5（比例矛盾片）", 300, -300, "B_MAIN", 400, None),
            ("G6", "网格 G6（人口修订片）", 200, -100, "B_MAIN", 1000, 1200),
            ("G7", "网格 G7（扩展区片）", 5200, 500, "B_EXT", 600, None),
            ("G8", "网格 G8（东站片，待恢复）", 3200, 200, "B_MAIN", 700, None),
            ("GR1", "网格 GR1（河岸村，质心跨河）", 400, -700, "B_MAIN", 650, None),
            ("GR2", "网格 GR2（南堰村，村级质心）", 1480, -820, "B_MAIN", 900, None),
            ("GR3", "网格 GR3（误标片，待人工纠正）", -950, 50, "B_MAIN", 450, None),
            ("GR4", "网格 GR4（断头路与主干路双候选）", 2250, 1080, "B_MAIN", 620, None),
            ("GR5", "网格 GR5（东站引道片，弱证据）", 3300, -300, "B_MAIN", 750, None),
        ]
        cur.executemany(
            "INSERT INTO pop_grids (grid_id, name, geom_x, geom_y, boundary_id, source_doc_id) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            [(gid, name, x, y, b, doc_v1) for gid, name, x, y, b, _, _ in grids],
        )
        rev_rows = []
        for gid, _, _, _, _, p1, p2 in grids:
            rev_rows.append((gid, 1, p1, date(2026, 6, 1), doc_v1))
            if p2 is not None:
                rev_rows.append((gid, 2, p2, date(2026, 7, 1), doc_v2))
        cur.executemany(
            """INSERT INTO pop_grid_revisions (grid_id, revision_no, population, as_of_date, source_doc_id)
               VALUES (%s,%s,%s,%s,%s)""",
            rev_rows,
        )


def _seed_connection_evidence(conn) -> None:
    """网格—路网候选连接点（证据治理模型）：每个候选带定位方法、误差范围、
    原始定位点、道路版本与空间质检标记；人工确认记录初始为空。

    候选的道路版本统一锚定 C1 路网版本；初始证据集不在此种子里物化版本，
    首次使用时由 get_current_connection_version 统一物化（保证大区样例追加后
    仍落入同一初始版本）。
    """
    from .services.evidence import ingest_candidate
    from .services.snapshot import get_or_create_network_version

    with conn.cursor() as cur:
        conn_doc = _add_doc(
            cur, "connection_evidence", "网格—路网连接与人口分配比例调查", "比例：无量纲，互斥子份额",
            datetime(2026, 6, 20, tzinfo=UTC), C1,
            raw="多出口网格按实测出口选择比例切分，份额互斥；比例之和不足 1 的缺口记为分配依据缺失",
        )
        village_doc = _add_doc(
            cur, "connection_evidence", "村级质心低精度定位成果", "定位方法：村级质心；误差：百米级",
            datetime(2026, 6, 25, tzinfo=UTC), C1,
            raw="沿河分散居住网格仅有村级质心，精度低，跨河候选需人工核实后方可作为接入依据",
        )
        addr_doc = _add_doc(
            cur, "connection_evidence", "地址标准化接入成果", "定位方法：地址标准化；误差：数十米级",
            datetime(2026, 6, 26, tzinfo=UTC), C1,
            raw="门牌地址标准化落点；个别记录存在同名误标，需人工审核确认",
        )
        conn.commit()

    road_version = get_or_create_network_version(conn, C1)
    rv = road_version["version_id"]

    # (grid, node, method, error_m, raw_x, raw_y, ratio, doc)
    candidates = [
        # —— 基线网格：调查成果（人工落点，小误差，互斥比例）——
        ("G1", "N6", "manual_placement", 25, 2000, 800, 1.0, conn_doc),
        ("G2", "N2", "manual_placement", 25, 1000, 500, 0.6, conn_doc),
        ("G2", "N5", "manual_placement", 25, 1000, 500, 0.4, conn_doc),
        # G3 无任何候选 → unknown_no_connector
        ("G4", "N1", "manual_placement", 25, 200, 200, 0.7, conn_doc),
        ("G5", "N1", "manual_placement", 25, 300, -300, 0.6, conn_doc),
        ("G5", "N2", "manual_placement", 25, 300, -300, 0.6, conn_doc),
        ("G6", "N1", "manual_placement", 25, 200, -100, 1.0, conn_doc),
        ("G7", "N5", "manual_placement", 25, 5200, 500, 1.0, conn_doc),
        ("G8", "N4", "manual_placement", 25, 3200, 200, 1.0, conn_doc),
        # —— GR1：低精度村级质心跨河指向 *直线距离最近* 的 N1（自动排除）+ 地址标准化接同侧 N9 ——
        ("GR1", "N1", "village_centroid", 600, 400, -700, None, village_doc),
        ("GR1", "N9", "address_standardization", 30, 400, -700, 1.0, addr_doc),
        # —— GR2：仅村级质心弱证据 → 整格连接不确定 ——
        ("GR2", "N10", "village_centroid", 800, 1480, -820, None, village_doc),
        # —— GR3：地址标准化误标到孤屯 N8（确定不可达），待人工纠正 ——
        ("GR3", "N8", "address_standardization", 80, -950, 50, 1.0, addr_doc),
        # —— GR4：最近直线距离是断头路 N11，主干路候选在 N6；均未配互斥权重 ——
        ("GR4", "N11", "address_standardization", 60, 2250, 1080, None, addr_doc),
        ("GR4", "N6", "address_standardization", 60, 2250, 1080, None, addr_doc),
        # —— GR5：弱证据候选接 N4，E7 恢复与否决定上界 ——
        ("GR5", "N4", "village_centroid", 450, 3300, -300, None, village_doc),
    ]
    for grid_id, node_id, method, err, x, y, ratio, doc in candidates:
        ingest_candidate(
            conn,
            grid_id=grid_id, node_id=node_id, locate_method=method,
            error_radius_m=err, geom_x=x, geom_y=y,
            allocation_ratio=ratio, road_version_id=rv, source_doc_id=doc,
        )
    conn.commit()


# ----------------------------------------------------------- 较大样例 --
def apply_c2_revision(conn) -> dict:
    """第二道路资料截止点 C2 的修订：E7 恢复重开 + E2 新增临时封闭。

    用于验证：
      - 此前不可达的 G8 因重开恢复覆盖（恢复候选机制）；
      - 新封路立即参与即时计算，并沿依赖边定位受影响任务；
      - 同截止点迟到修订产生新路网版本，旧任务不会覆盖新版本索引。
    """
    with conn.cursor() as cur:
        doc = _add_doc(
            cur, "road_event", "暴雨交通管制通告（第 2 号·修订）",
            "时间：本地时刻(UTC)；速度：km/h",
            C2, C2, revision=2,
            raw="E7 东站引道 08:40 恢复通行；E2 中心市场至河东桥 08:15-08:35 临时封闭",
        )
        # EV3 追加第 2 修订行；第 1 行（未恢复封路）保留，C1 截止点分析仍可精确复现
        cur.execute(
            """INSERT INTO road_events
                   (event_id, revision_no, event_type, edge_id, effective_from, effective_to,
                    speed_limit_kmh, supersedes_event_id, status, source_doc_id, received_at)
               VALUES ('EV3',2,'closure','E7',%s,%s,NULL,'EV3','active',%s,%s)""",
            (_dt(0), _dt(40), doc, C2 - timedelta(minutes=2)),
        )
        cur.execute(
            """INSERT INTO road_event_revisions
                   (event_id, revision_no, change_summary, change_json, source_doc_id)
               VALUES ('EV3',2,%s,%s,%s)""",
            ("恢复修订：明确 08:40 重开",
             Json({"effective_to": "08:40", "previous_effective_to": None}), doc),
        )
        cur.execute(
            "INSERT INTO road_event_registry (event_id, edge_id, event_type) "
            "VALUES ('EV4','E2','closure')"
        )
        cur.execute(
            """INSERT INTO road_events
                   (event_id, revision_no, event_type, edge_id, effective_from, effective_to,
                    speed_limit_kmh, status, source_doc_id, received_at)
               VALUES ('EV4',1,'closure','E2',%s,%s,NULL,'active',%s,%s)""",
            (_dt(15), _dt(35), doc, C2 - timedelta(minutes=2)),
        )
        cur.execute(
            """INSERT INTO road_event_revisions
                   (event_id, revision_no, change_summary, change_json, source_doc_id)
               VALUES ('EV4',1,%s,%s,%s)""",
            ("首发：E2 临时封闭 08:15-08:35",
             Json({"edge_id": "E2", "effective_from": "08:15", "effective_to": "08:35"}), doc),
        )
        conn.commit()

    # 修订涉及 E7（重开）与 E2（新封路）：沿依赖边失效，并筛查可能因重开恢复的区域
    from .services.compute import manager

    inv_e7 = manager.invalidate_for_edge(conn, "E7", "C2 修订：E7 恢复重开")
    inv_e2 = manager.invalidate_for_edge(conn, "E2", "C2 修订：E2 新增临时封闭")
    return {
        "cutoff_at": C2,
        "changes": ["EV3 重开 08:40", "EV4 新封路 E2 08:15-08:35"],
        "invalidation": {"E7": inv_e7, "E2": inv_e2},
    }


def seed_large(conn) -> None:
    """在小型样例之外追加 8×8 规则路网与 40 个网格（固定模式，无随机数）。"""
    with conn.cursor() as cur:
        doc = _add_doc(cur, "road_node", "区级骨干路网节点台账（大区样例）", "坐标单位：米",
                       datetime(2026, 5, 1, tzinfo=UTC), C1,
                       raw="8×8 规则网格路网，节点 Lxy；奇数行设为单向交通组织")
        for i in range(8):
            for j in range(8):
                nid = f"L{i}{j}"
                cur.execute(
                    "INSERT INTO road_nodes (node_id, name, geom_x, geom_y, allow_wait, source_doc_id) "
                    "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (node_id) DO NOTHING",
                    (nid, f"大区节点 {i},{j}", 10000 + j * 800, 10000 + i * 800,
                     (i + j) % 2 == 0, doc),
                )
        edge_rows = []
        eid = 0
        for i in range(8):
            for j in range(8):
                # 横向：奇数行只允许向东（单向边演示）；偶数行双向
                if j < 7:
                    edge_rows.append((f"LE{eid}", f"L{i}{j}", f"L{i}{j+1}")); eid += 1
                    if i % 2 == 0:
                        edge_rows.append((f"LE{eid}", f"L{i}{j+1}", f"L{i}{j}")); eid += 1
                # 纵向双向
                if i < 7:
                    edge_rows.append((f"LE{eid}", f"L{i}{j}", f"L{i+1}{j}")); eid += 1
                    edge_rows.append((f"LE{eid}", f"L{i+1}{j}", f"L{i}{j}")); eid += 1
        cur.executemany(
            """INSERT INTO road_edge_revisions
                   (edge_id, revision_no, from_node, to_node, length_m, base_speed_kmh,
                    valid_from, source_doc_id)
               VALUES (%s,1,%s,%s,800,40,%s,%s)
               ON CONFLICT (edge_id, revision_no) DO NOTHING""",
            [(e, f, t, datetime(2026, 5, 1, tzinfo=UTC), doc) for e, f, t in edge_rows],
        )

        # 固定模式事件：每两行一条分时限速、一条中段封闭
        for i in range(0, 8, 2):
            edge = next(e for e, f, t in edge_rows if f == f"L{i}3" and t == f"L{i}4")
            eid = f"LEV-S{i}"
            cur.execute(
                "INSERT INTO road_event_registry (event_id, edge_id, event_type) "
                "VALUES (%s,%s,'speed_limit') ON CONFLICT (event_id) DO NOTHING",
                (eid, edge),
            )
            cur.execute(
                """INSERT INTO road_events
                       (event_id, revision_no, event_type, edge_id, effective_from, effective_to,
                        speed_limit_kmh, status, source_doc_id, received_at)
                   VALUES (%s,1,'speed_limit',%s,%s,%s,15,'active',%s,%s)
                   ON CONFLICT (event_id, revision_no) DO NOTHING""",
                (eid, edge, _dt(0), _dt(90), doc, C1 - timedelta(minutes=10)),
            )
        for i in (1, 5):
            edge = next(e for e, f, t in edge_rows if f == f"L{i}5" and t == f"L{i}6")
            eid = f"LEV-C{i}"
            cur.execute(
                "INSERT INTO road_event_registry (event_id, edge_id, event_type) "
                "VALUES (%s,%s,'closure') ON CONFLICT (event_id) DO NOTHING",
                (eid, edge),
            )
            cur.execute(
                """INSERT INTO road_events
                       (event_id, revision_no, event_type, edge_id, effective_from, effective_to,
                        speed_limit_kmh, status, source_doc_id, received_at)
                   VALUES (%s,1,'closure',%s,%s,%s,NULL,'active',%s,%s)
                   ON CONFLICT (event_id, revision_no) DO NOTHING""",
                (eid, edge, _dt(10), _dt(70), doc, C1 - timedelta(minutes=10)),
            )

        # 四个角安置点
        for k, (i, j) in enumerate(((0, 0), (0, 7), (7, 0), (7, 7))):
            sid = f"SL{k}"
            cur.execute(
                "INSERT INTO shelters (shelter_id, name, node_id, geom_x, geom_y, source_doc_id) "
                "VALUES (%s,%s,%s,%s,%s,%s) ON CONFLICT (shelter_id) DO NOTHING",
                (sid, f"大区安置点 {k}", f"L{i}{j}", 10000 + j * 800, 10000 + i * 800, doc),
            )
            cur.execute(
                "INSERT INTO shelter_open_windows (shelter_id, open_from, open_to) "
                "VALUES (%s,%s,%s) ON CONFLICT DO NOTHING",
                (sid, datetime(2026, 7, 15, 6, tzinfo=UTC), datetime(2026, 7, 15, 20, tzinfo=UTC)),
            )

        # 40 个网格，连接到确定性的节点对；第 40 个刻意无连接 → 未知
        from .services.evidence import ingest_candidate
        from .services.snapshot import get_or_create_network_version

        rv = get_or_create_network_version(conn, C1)["version_id"]
        for k in range(40):
            gid = f"GL{k:02d}"
            i, j = divmod(k, 8)
            cur.execute(
                "INSERT INTO pop_grids (grid_id, name, geom_x, geom_y, boundary_id, source_doc_id) "
                "VALUES (%s,%s,%s,%s,'B_MAIN',%s) ON CONFLICT (grid_id) DO NOTHING",
                (gid, f"大区网格 {k}", 10000 + j * 800, 9700 + i * 800, doc),
            )
            cur.execute(
                "INSERT INTO pop_grid_revisions (grid_id, revision_no, population, as_of_date, source_doc_id) "
                "VALUES (%s,1,%s,%s,%s) ON CONFLICT DO NOTHING",
                (gid, 500 + (k * 37) % 900, date(2026, 6, 1), doc),
            )
            if k != 39:
                ingest_candidate(
                    conn,
                    grid_id=gid, node_id=f"L{i}{j}", locate_method="manual_placement",
                    error_radius_m=25, geom_x=10000 + j * 800, geom_y=9700 + i * 800,
                    allocation_ratio=1.0, road_version_id=rv, source_doc_id=doc,
                )
    conn.commit()


if __name__ == "__main__":
    from .db import get_conn, init_db

    init_db()
    with get_conn() as connection:
        reseed(connection)
        seed_large(connection)
    print("seed completed: small hand-computable network + large 8x8 sample")
