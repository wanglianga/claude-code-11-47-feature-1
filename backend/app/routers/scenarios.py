"""情景工作区、覆盖结果、下钻查询与查看者生命周期。"""
from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException

from ..schemas_api import ScenarioCreate, ViewerRelease
from ..services.compute import manager
from ..services.connection import resolve_connections
from .deps import get_db, jsonable

router = APIRouter(tags=["情景与覆盖"])


@router.post("/scenarios")
def create_scenario(payload: ScenarioCreate, conn=Depends(get_db)):
    """创建（或按完整签名复用）情景并同步得到覆盖面。"""
    result = manager.get_or_create(
        conn,
        name=payload.name,
        population_as_of=payload.population_as_of,
        departure_time=payload.departure_time,
        max_trip_minutes=payload.max_trip_minutes,
        road_cutoff=payload.road_cutoff,
        viewer_id=payload.viewer_id,
        kind="on_demand",
        note=payload.note,
        boundary_id=payload.boundary_id,
    )
    return jsonable(result)


@router.get("/scenarios")
def list_scenarios(conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT s.*, t.task_id, t.status AS task_status, t.stale AS task_stale,
                      t.kind AS task_kind, t.ref_count, t.evidence_cutoff_at AS task_evidence
               FROM scenarios s
               LEFT JOIN compute_tasks t ON t.scenario_id = s.scenario_id
               ORDER BY s.created_at DESC"""
        )
        rows = cur.fetchall()
    return jsonable({"scenarios": rows})


@router.get("/tasks/{task_id}/summary")
def task_summary(task_id: str, conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM compute_tasks WHERE task_id=%s", (task_id,))
        task = cur.fetchone()
        if task is None:
            raise HTTPException(404, "任务不存在")
        cur.execute("SELECT * FROM coverage_summaries WHERE task_id=%s", (task_id,))
        summary = cur.fetchone()
    return jsonable({"task": task, "summary": summary})


@router.get("/tasks/{task_id}/grids")
def task_grids(task_id: str, conn=Depends(get_db)):
    """网格份额明细：状态、份额人口、到发时刻、安置点、等待成本、排除原因。"""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT r.*, g.name AS grid_name, g.geom_x, g.geom_y, g.boundary_id
               FROM grid_share_results r
               JOIN pop_grids g ON g.grid_id = r.grid_id
               WHERE r.task_id=%s
               ORDER BY r.grid_id, r.share_index""",
            (task_id,),
        )
        rows = cur.fetchall()
    return jsonable({"shares": rows})


@router.get("/tasks/{task_id}/arrivals")
def task_arrivals(task_id: str, conn=Depends(get_db)):
    """节点到达时间与见证路径（含等待段聚合）。"""
    with conn.cursor() as cur:
        cur.execute(
            """SELECT a.*, n.geom_x, n.geom_y, n.allow_wait
               FROM node_arrivals a
               JOIN road_nodes n ON n.node_id = a.node_id
               WHERE a.task_id=%s ORDER BY a.arrival_time""",
            (task_id,),
        )
        rows = cur.fetchall()
    return jsonable({"arrivals": rows})


@router.get("/tasks/{task_id}/witness")
def task_witness(task_id: str, grid_id: str, conn=Depends(get_db)):
    """单网格下钻：份额、候选连接点、排除原因、到发时刻、见证边路径与上下界影响。"""
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM grid_share_results WHERE task_id=%s AND grid_id=%s "
            "ORDER BY share_index",
            (task_id, grid_id),
        )
        shares = cur.fetchall()
        if not shares:
            raise HTTPException(404, "该任务下没有此网格的份额结果")
        node_ids = {s["connector_node"] for s in shares if s["connector_node"]}
        arrivals = []
        for nid in node_ids:
            cur.execute(
                "SELECT * FROM node_arrivals WHERE task_id=%s AND node_id=%s",
                (task_id, nid),
            )
            arrivals.append(cur.fetchone())
        cur.execute(
            "SELECT * FROM task_edge_dependencies WHERE task_id=%s ORDER BY edge_id",
            (task_id,),
        )
        deps = cur.fetchall()
        # 连接不确定块的候选路径评估留痕
        cur.execute(
            "SELECT * FROM grid_candidate_results WHERE task_id=%s AND grid_id=%s "
            "ORDER BY candidate_id",
            (task_id, grid_id),
        )
        candidate_results = cur.fetchall()
        # 任务固定的连接证据版本（结果解释必须回到同一版本）
        cur.execute(
            """SELECT s.connection_version_id
               FROM compute_tasks t JOIN scenarios s ON s.scenario_id = t.scenario_id
               WHERE t.task_id=%s""",
            (task_id,),
        )
        row = cur.fetchone()
        connection_version_id = row["connection_version_id"] if row else None
        # 该网格不确定块对总覆盖上下界的影响
        cur.execute(
            """SELECT covered_population, covered_upper_population,
                      uncertain_population, uncertain_potentially_covered
               FROM coverage_summaries WHERE task_id=%s""",
            (task_id,),
        )
        summary = cur.fetchone()

    candidates = []
    if connection_version_id is not None:
        state = resolve_connections(conn, connection_version_id).get(grid_id)
        if state is not None:
            candidates = state["candidates"]

    uncertain_block = next((s for s in shares if s["status"] == "uncertain"), None)
    bounds_impact = None
    if summary is not None and uncertain_block is not None:
        any_reachable = any(c["reachable"] for c in candidate_results)
        bounds_impact = {
            "grid_uncertain_population": uncertain_block["share_population"],
            "counts_toward_upper_bound": bool(any_reachable),
            "task_covered_lower": summary["covered_population"],
            "task_covered_upper": summary["covered_upper_population"],
            "task_uncertain_population": summary["uncertain_population"],
            "task_uncertain_potentially_covered": summary["uncertain_potentially_covered"],
        }

    return jsonable({
        "grid_id": grid_id,
        "shares": shares,
        "connector_arrivals": arrivals,
        "task_edge_dependencies": deps,
        "candidate_results": candidate_results,
        "connection_version_id": connection_version_id,
        "candidates": candidates,
        "bounds_impact": bounds_impact,
    })


@router.post("/tasks/{task_id}/release")
def release_viewer(task_id: str, payload: ViewerRelease, conn=Depends(get_db)):
    """查看者离开：只减引用，不取消其他查看者仍需要的任务。"""
    return jsonable(manager.release_viewer(conn, task_id, payload.viewer_id))


@router.get("/tasks")
def list_tasks(conn=Depends(get_db)):
    with conn.cursor() as cur:
        cur.execute(
            """SELECT t.*, s.name AS scenario_name
               FROM compute_tasks t JOIN scenarios s ON s.scenario_id=t.scenario_id
               ORDER BY t.created_at DESC"""
        )
        tasks = cur.fetchall()
        cur.execute("SELECT * FROM coverage_cache_index ORDER BY computed_at DESC")
        cache = cur.fetchall()
    return jsonable({"tasks": tasks, "cache_index": cache})
