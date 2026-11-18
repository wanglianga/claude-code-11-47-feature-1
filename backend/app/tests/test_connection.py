"""连接证据治理测试：河流阻隔、村级质心、人工纠正、多候选上下界、
道路修复重算，以及人口总量守恒。

对应验收口径：
- 低精度质心跨越河流或候选分落断头路/主干路时，不默认使用最近直线距离的道路；
- 人工确认在情景计算途中到达只形成后续连接版本，不改写已发布情景；
- 同一网格多个候选只按互斥份额或上、下界汇总，不把整格人口重复加入多个安置点；
- 总人口 = 确定可达 + 确定不可达 + 连接不确定 + 未知（每个情景恒等）。
"""
from datetime import date, timedelta

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.seed import C1, C2, T0, apply_c2_revision
from app.services.compute import manager
from app.services.comparison import publish_comparison
from app.services.connection import (
    apply_review_batch,
    get_current_connection_version,
    resolve_connections,
)

AS_OF = date(2026, 6, 1)
client = TestClient(app)


def _run(db, *, name, cutoff=C1, departure=T0, minutes=30, as_of=AS_OF, boundary=None):
    return manager.get_or_create(
        db, name=name, population_as_of=as_of, departure_time=departure,
        max_trip_minutes=minutes, road_cutoff=cutoff, viewer_id="pytest-conn",
        boundary_id=boundary,
    )


def _shares(db, task_id, grid_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM grid_share_results WHERE task_id=%s AND grid_id=%s ORDER BY share_index",
            (task_id, grid_id),
        )
        return cur.fetchall()


def _summary(db, task_id):
    with db.cursor() as cur:
        cur.execute("SELECT * FROM coverage_summaries WHERE task_id=%s", (task_id,))
        return cur.fetchone()


def _candidate_of(db, grid_id, node_id):
    with db.cursor() as cur:
        cur.execute(
            "SELECT * FROM grid_connection_candidates WHERE grid_id=%s AND node_id=%s",
            (grid_id, node_id),
        )
        return cur.fetchone()


def _assert_conservation(summary):
    assert abs(
        summary["covered_population"] + summary["uncovered_population"]
        + summary["uncertain_population"] + summary["unknown_population"]
        - summary["total_population"]
    ) < 1e-6
    assert abs(
        summary["covered_upper_population"]
        - (summary["covered_population"] + summary["uncertain_potentially_covered"])
    ) < 1e-6


# ----------------------------------------------------------- R1 河流阻隔 --
def test_river_crossing_village_centroid_never_defaulted_to_nearest_road(db):
    """GR1：直线距离最近的道路（N1，跨河）来自低精度村级质心 → 自动排除，
    确定分类只能落在经核实的同侧候选 N9 上。"""
    version = get_current_connection_version(db)
    state = resolve_connections(db, version["version_id"])["GR1"]
    by_node = {c["node_id"]: c for c in state["candidates"]}
    # 跨河低精度候选被自动排除，且原因明示禁止按最近距离默认
    assert by_node["N1"]["disposition"] == "excluded"
    assert "跨" in by_node["N1"]["disposition_reason"] and "最近" in by_node["N1"]["disposition_reason"]
    assert by_node["N1"]["crosses_barrier"] is True
    # N1 恰是直线距离最近的候选（若按最近距离默认必选它）
    nearest = min(state["candidates"], key=lambda c: c["distance_to_node_m"])
    assert nearest["node_id"] == "N1"
    # 有效份额只来自同侧核实候选 N9
    assert state["shares"] == [
        {"candidate_id": by_node["N9"]["candidate_id"], "node_id": "N9", "ratio": 1.0}
    ]

    r = _run(db, name="河流阻隔")
    gr1 = _shares(db, r["task_id"], "GR1")
    assert len(gr1) == 1 and gr1[0]["status"] == "covered"
    assert gr1[0]["connector_node"] == "N9" and gr1[0]["share_population"] == 650.0
    _assert_conservation(_summary(db, r["task_id"]))


def test_all_candidates_excluded_becomes_unknown_not_silent_nearest(db):
    """GR1 的 N9 候选被人工排除后：没有任何可用接入假设 → 整格未知，
    系统绝不回退到跨河的最近距离候选。"""
    cand_n9 = _candidate_of(db, "GR1", "N9")
    apply_review_batch(
        db, [{"candidate_id": cand_n9["candidate_id"], "action": "reject",
              "note": "实地核查：该片不接入 N9"}], reviewer="pytest",
    )
    r = _run(db, name="全部排除")
    gr1 = _shares(db, r["task_id"], "GR1")
    assert len(gr1) == 1 and gr1[0]["status"] == "unknown"
    assert "no_plausible_candidate" in gr1[0]["reason"]
    assert gr1[0]["share_population"] == 650.0  # 人口完整落入未知，不丢失
    _assert_conservation(_summary(db, r["task_id"]))


# ------------------------------------------------------- R1 断头路/主干路 --
def test_dead_end_vs_arterial_candidates_form_bounds_not_nearest_default(db):
    """GR4：最近直线距离候选是断头路 N11，主干路候选在 N6；两者均未配互斥
    权重 → 整格连接不确定，人口只计一次，上界含 N6 可达性。"""
    version = get_current_connection_version(db)
    state = resolve_connections(db, version["version_id"])["GR4"]
    by_node = {c["node_id"]: c for c in state["candidates"]}
    assert by_node["N11"]["road_class"] == "dead_end"
    assert by_node["N6"]["road_class"] == "arterial"
    nearest = min(state["candidates"], key=lambda c: c["distance_to_node_m"])
    assert nearest["node_id"] == "N11"  # 最近的是断头路，但系统不默认它
    assert state["shares"] == [] and len(state["ambiguous"]) == 2

    r = _run(db, name="断头路与主干路")
    gr4 = _shares(db, r["task_id"], "GR4")
    assert len(gr4) == 1 and gr4[0]["status"] == "uncertain"
    assert gr4[0]["share_population"] == 620.0  # 整格人口只计一次
    with db.cursor() as cur:
        cur.execute(
            "SELECT node_id, reachable, shelter_id FROM grid_candidate_results "
            "WHERE task_id=%s AND grid_id='GR4' ORDER BY node_id",
            (r["task_id"],),
        )
        evals = {row["node_id"]: row for row in cur.fetchall()}
    assert evals["N11"]["reachable"] is False       # 断头路确实不可达
    assert evals["N6"]["reachable"] is True and evals["N6"]["shelter_id"] == "S2"
    s = _summary(db, r["task_id"])
    assert s["uncertain_potentially_covered"] >= 620.0  # 上界含该片，下界不含
    _assert_conservation(s)


# ----------------------------------------------------- 村级质心与人工确认 --
def test_village_centroid_uncertain_until_confirmed_then_new_version_only(db):
    """GR2：村级质心弱证据 → 连接不确定；人工确认后只形成后续连接版本，
    旧情景结果不被改写（版本引用隔离）。"""
    v1 = get_current_connection_version(db)
    before = _run(db, name="质心确认前")
    gr2_before = _shares(db, before["task_id"], "GR2")
    assert gr2_before[0]["status"] == "uncertain"
    assert gr2_before[0]["share_population"] == 900.0
    s_before = _summary(db, before["task_id"])
    assert s_before["uncertain_potentially_covered"] >= 900.0  # 上界含，下界不含

    # 计算途中到达的人工确认 → 后续连接版本
    cand = _candidate_of(db, "GR2", "N10")
    out = apply_review_batch(
        db, [{"candidate_id": cand["candidate_id"], "action": "confirm",
              "ratio_override": 1.0, "note": "实地核实接入南堰村口"}],
        reviewer="pytest",
    )
    v2 = out["version"]
    assert v2["version_id"] > v1["version_id"]

    # 同参数新情景 → 新签名新任务，GR2 转为确定可达
    after = _run(db, name="质心确认后")
    assert after["task_id"] != before["task_id"]
    gr2_after = _shares(db, after["task_id"], "GR2")
    assert gr2_after[0]["status"] == "covered"
    assert gr2_after[0]["share_population"] == 900.0
    # 旧任务结果仍固定在旧版本：重读旧任务份额，依然是 uncertain
    gr2_old = _shares(db, before["task_id"], "GR2")
    assert gr2_old[0]["status"] == "uncertain"
    _assert_conservation(_summary(db, after["task_id"]))


# ------------------------------------------------------------- 人工纠正 --
def test_manual_correction_forms_new_version_and_preserves_publication(db):
    """GR3：误标到孤屯 N8（确定不可达）→ 发布 → 人工纠正接入 N3 →
    新连接版本下确定可达；已发布结果的输入清单仍固定在旧版本。"""
    v1 = get_current_connection_version(db)
    a = _run(db, name="纠正前", minutes=30)
    gr3 = _shares(db, a["task_id"], "GR3")
    assert gr3[0]["status"] == "uncovered" and gr3[0]["connector_node"] == "N8"

    b = _run(db, name="对照组", minutes=45, departure=T0 + timedelta(minutes=5))
    pub = publish_comparison(db, a["task_id"], b["task_id"], "纠正前发布", "pytest")
    assert pub["input_manifest"]["scenario_a"]["connection_version_id"] == v1["version_id"]

    # 人工纠正：接入新候选 N3 + 排除误标候选 + 确认新候选（整批一个新版本）
    from app.services.evidence import ingest_candidate

    new_cand = ingest_candidate(
        db, grid_id="GR3", node_id="N3", locate_method="manual_placement",
        error_radius_m=20, geom_x=2010, geom_y=40, allocation_ratio=None,
    )
    old_cand = _candidate_of(db, "GR3", "N8")
    out = apply_review_batch(
        db,
        [
            {"candidate_id": old_cand["candidate_id"], "action": "reject",
             "note": "同名误标，实际不在孤屯"},
            {"candidate_id": new_cand["candidate_id"], "action": "confirm",
             "ratio_override": 1.0, "note": "人工落点确认接入河东桥头"},
        ],
        reviewer="pytest",
    )
    assert out["version"]["version_id"] > v1["version_id"]

    corrected = _run(db, name="纠正后", minutes=30)
    assert corrected["task_id"] != a["task_id"]
    gr3_fixed = _shares(db, corrected["task_id"], "GR3")
    assert gr3_fixed[0]["status"] == "covered"
    assert gr3_fixed[0]["connector_node"] == "N3" and gr3_fixed[0]["shelter_id"] == "S1"
    # 已发布结果与旧任务不被改写
    gr3_old = _shares(db, a["task_id"], "GR3")
    assert gr3_old[0]["status"] == "uncovered"
    with db.cursor() as cur:
        cur.execute("SELECT input_manifest FROM publications WHERE publication_id=%s",
                    (pub["publication"]["publication_id"],))
        manifest = cur.fetchone()["input_manifest"]
    assert manifest["scenario_a"]["connection_version_id"] == v1["version_id"]
    _assert_conservation(_summary(db, corrected["task_id"]))


# ------------------------------------------- 多候选不重复计数（人口守恒） --
def test_multiple_candidates_never_double_count_population(db):
    """GR4 确认 N6 后：620 人只计一次进入确定可达；不确定量相应减少，
    总量恒等式在每个版本下都成立。"""
    before = _run(db, name="多候选确认前")
    s_before = _summary(db, before["task_id"])
    assert s_before["uncertain_population"] >= 620.0

    cand = _candidate_of(db, "GR4", "N6")
    apply_review_batch(
        db, [{"candidate_id": cand["candidate_id"], "action": "confirm",
              "ratio_override": 1.0, "note": "确认主干路接入"}],
        reviewer="pytest",
    )
    after = _run(db, name="多候选确认后")
    gr4 = _shares(db, after["task_id"], "GR4")
    covered_rows = [x for x in gr4 if x["status"] == "covered"]
    assert len(covered_rows) == 1 and covered_rows[0]["share_population"] == 620.0
    s_after = _summary(db, after["task_id"])
    # 确定可达恰好增加 620（不是 620×候选数），连接不确定恰好减少 620
    assert abs(s_after["covered_population"] - s_before["covered_population"] - 620.0) < 1e-6
    assert abs(s_before["uncertain_population"] - s_after["uncertain_population"] - 620.0) < 1e-6
    _assert_conservation(s_after)


# ------------------------------------------------- 道路修复后的重新计算 --
def test_road_repair_recomputes_uncertain_upper_bound(fresh_db):
    """GR5：C1 时 E7 未恢复 → 不确定且上界贡献为 0；C2 重开 + 60 分钟时限 →
    上界包含该片 750 人；人口总量在两个截止点都守恒。"""
    db = fresh_db
    c1 = _run(db, name="修复前", cutoff=C1, minutes=60)
    gr5_c1 = _shares(db, c1["task_id"], "GR5")
    assert gr5_c1[0]["status"] == "uncertain"
    with db.cursor() as cur:
        cur.execute(
            "SELECT reachable FROM grid_candidate_results WHERE task_id=%s AND grid_id='GR5'",
            (c1["task_id"],),
        )
        assert cur.fetchone()["reachable"] is False
    s_c1 = _summary(db, c1["task_id"])
    pot_c1 = s_c1["uncertain_potentially_covered"]
    _assert_conservation(s_c1)

    apply_c2_revision(db)
    c2 = _run(db, name="修复后", cutoff=C2, minutes=60)
    gr5_c2 = _shares(db, c2["task_id"], "GR5")
    assert gr5_c2[0]["status"] == "uncertain"  # 证据仍弱：不自动升级为确定
    with db.cursor() as cur:
        cur.execute(
            "SELECT reachable, shelter_id FROM grid_candidate_results "
            "WHERE task_id=%s AND grid_id='GR5'",
            (c2["task_id"],),
        )
        row = cur.fetchone()
        assert row["reachable"] is True and row["shelter_id"] == "S3"
    s_c2 = _summary(db, c2["task_id"])
    # 上界增量恰好包含 GR5 的 750 人（G8 的 700 进入确定可达，不进上界增量）
    assert abs((s_c2["uncertain_potentially_covered"] - pot_c1) - 750.0) < 1e-6
    _assert_conservation(s_c2)


# ------------------------------------------------------------- API 端到端 --
def test_connection_governance_api_end_to_end(fresh_db):
    """HTTP：候选接入 → 审核批次 → 版本演进 → 网格概览 → 情景下钻候选评估。"""
    payload = {
        "name": "API-连接证据", "population_as_of": "2026-06-01",
        "departure_time": T0.isoformat(), "max_trip_minutes": 30,
        "road_cutoff": C1.isoformat(), "viewer_id": "e2e-conn",
    }
    r1 = client.post("/scenarios", json=payload).json()
    v1 = client.get("/connections/version").json()["current"]

    # 接入 GR2 的人工落点候选并确认（两个动作两个版本，版本号单调递增）
    cand = client.post("/connections/candidates", json={
        "grid_id": "GR2", "node_id": "N10", "locate_method": "manual_placement",
        "error_radius_m": 15, "geom_x": 1500, "geom_y": -790,
        "source_title": "现场核查落点",
    }).json()
    assert cand["version"]["version_id"] > v1["version_id"]
    rev = client.post("/connections/reviews", json={
        "reviews": [{"candidate_id": cand["candidate"]["candidate_id"],
                     "action": "confirm", "ratio_override": 1.0,
                     "note": "现场核实"}],
        "reviewer": "e2e",
    }).json()
    assert rev["version"]["version_id"] > cand["version"]["version_id"]

    # 网格概览：GR2 出现证据充分的候选
    overview = client.get("/connections/grids").json()
    gr2 = next(g for g in overview["grids"] if g["grid_id"] == "GR2")
    assert gr2["effective_count"] >= 1 and gr2["ratio_sum"] >= 1.0
    detail = client.get("/connections/candidates", params={"grid_id": "GR2"}).json()
    assert any(c["review_action"] == "confirm" for c in detail["candidates"])

    # 同参数新情景 → 新任务；下钻可见候选评估与上下界影响
    r2 = client.post("/scenarios", json=payload).json()
    assert r2["task_id"] != r1["task_id"]
    witness = client.get(f"/tasks/{r2['task_id']}/witness", params={"grid_id": "GR2"}).json()
    assert witness["connection_version_id"] == rev["version"]["version_id"]
    assert witness["candidates"]
    summary = client.get(f"/tasks/{r2['task_id']}/summary").json()["summary"]
    assert abs(summary["covered_population"] + summary["uncovered_population"]
               + summary["uncertain_population"] + summary["unknown_population"]
               - summary["total_population"]) < 1e-6

    # 不确定网格的下钻：GR4 的候选评估与上下界影响完整可查
    witness4 = client.get(f"/tasks/{r2['task_id']}/witness", params={"grid_id": "GR4"}).json()
    assert {c["node_id"] for c in witness4["candidate_results"]} == {"N6", "N11"}
    assert witness4["bounds_impact"]["grid_uncertain_population"] == 620.0
    assert witness4["bounds_impact"]["counts_toward_upper_bound"] is True

    # 阻隔与版本列表可查
    barriers = client.get("/connections/barriers").json()["barriers"]
    assert any(b["barrier_id"] == "RIVER1" for b in barriers)
    versions = client.get("/connections/version").json()["versions"]
    assert len(versions) >= 3
