"""连接证据治理：候选接入、人工审核、证据版本与网格连接状态下钻。

结果解释职责：把“候选—审核—版本—网格状态”的链条完整暴露给前端，
使任何一块不确定网格都能查到候选点、排除原因与对总覆盖上下界的影响。
"""
from __future__ import annotations

from datetime import datetime, timezone

from fastapi import APIRouter, Depends, HTTPException

from ..schemas_api import CandidateIngest, ReviewBatchCreate
from ..seed import _add_doc
from ..services.connection import (
    apply_review_batch,
    get_current_connection_version,
    materialize_connection_version,
    resolve_connections,
)
from ..services.evidence import ingest_candidate, load_barriers
from .deps import get_db, jsonable

router = APIRouter(prefix="/connections", tags=["连接证据治理"])


@router.get("/version")
def current_version(conn=Depends(get_db)):
    """当前连接证据版本与历史版本列表。"""
    version = get_current_connection_version(conn)
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM connection_versions ORDER BY version_id DESC")
        versions = cur.fetchall()
        cur.execute("SELECT * FROM connection_reviews ORDER BY review_id DESC")
        reviews = cur.fetchall()
    return jsonable({"current": version, "versions": versions, "reviews": reviews})


@router.get("/grids")
def grid_connection_overview(conn=Depends(get_db)):
    """每个网格在当前版本下的候选处置概览（含无候选网格）。"""
    version = get_current_connection_version(conn)
    states = resolve_connections(conn, version["version_id"])
    with conn.cursor() as cur:
        cur.execute("SELECT grid_id, name, boundary_id FROM pop_grids ORDER BY grid_id")
        grids = cur.fetchall()
    out = []
    for g in grids:
        st = states.get(g["grid_id"])
        if st is None:
            out.append({
                "grid_id": g["grid_id"], "name": g["name"], "boundary_id": g["boundary_id"],
                "candidate_count": 0, "effective_count": 0, "weak_count": 0,
                "excluded_count": 0, "ratio_sum": 0.0,
                "unknown_code": "unknown_no_connector",
            })
            continue
        out.append({
            "grid_id": g["grid_id"], "name": g["name"], "boundary_id": g["boundary_id"],
            "candidate_count": len(st["candidates"]),
            "effective_count": sum(1 for c in st["candidates"] if c["disposition"] == "effective"),
            "weak_count": sum(1 for c in st["candidates"] if c["disposition"] == "weak"),
            "excluded_count": sum(1 for c in st["candidates"] if c["disposition"] == "excluded"),
            "ratio_sum": round(st["ratio_sum"], 6),
            "unknown_code": st["unknown_code"],
        })
    return jsonable({"version_id": version["version_id"], "grids": out})


@router.get("/candidates")
def list_candidates(grid_id: str, version_id: int | None = None, conn=Depends(get_db)):
    """单网格候选明细：定位方法、误差、道路版本、道路等级、跨河标记、审核与处置。"""
    if version_id is None:
        version_id = get_current_connection_version(conn)["version_id"]
    states = resolve_connections(conn, version_id)
    state = states.get(grid_id)
    if state is None:
        return jsonable({
            "version_id": version_id, "grid_id": grid_id, "candidates": [],
            "shares": [], "ratio_sum": 0.0, "unknown_code": "unknown_no_connector",
        })
    return jsonable({
        "version_id": version_id,
        "grid_id": grid_id,
        "candidates": state["candidates"],
        "shares": state["shares"],
        "ratio_sum": state["ratio_sum"],
        "unknown_code": state["unknown_code"],
    })


@router.post("/candidates")
def create_candidate(payload: CandidateIngest, conn=Depends(get_db)):
    """接入新候选连接点：空间质检（跨河/道路等级/距离）后落库并物化新版本。"""
    now = datetime.now(timezone.utc)
    with conn.cursor() as cur:
        doc_id = _add_doc(
            cur, "connection_evidence", payload.source_title,
            f"定位方法：{payload.locate_method}", now, now,
            raw=payload.raw_text or f"{payload.grid_id}→{payload.node_id} 候选接入",
        )
    try:
        row = ingest_candidate(
            conn,
            grid_id=payload.grid_id, node_id=payload.node_id,
            locate_method=payload.locate_method, error_radius_m=payload.error_radius_m,
            geom_x=payload.geom_x, geom_y=payload.geom_y,
            allocation_ratio=payload.allocation_ratio, source_doc_id=doc_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    version, _ = materialize_connection_version(
        conn, f"新候选接入（{payload.grid_id}→{payload.node_id}）"
    )
    return jsonable({"candidate": row, "version": version})


@router.post("/reviews")
def create_reviews(payload: ReviewBatchCreate, conn=Depends(get_db)):
    """人工审核批次：confirm/reject/adjust_ratio，整批物化为后续连接版本。

    已固定旧版本的情景、任务与发布记录不受新审核影响（版本引用隔离）。
    """
    try:
        result = apply_review_batch(
            conn,
            [r.model_dump() for r in payload.reviews],
            reviewer=payload.reviewer,
            note=payload.note,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    return jsonable(result)


@router.get("/barriers")
def barriers(conn=Depends(get_db)):
    """空间阻隔（河流）折线，供地图与候选质检对照。"""
    return jsonable({"barriers": load_barriers(conn)})
