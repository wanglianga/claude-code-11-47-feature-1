"""连接审核与证据版本：候选集合 + 人工确认记录的版本化解析。

职责边界：本模块负责“哪些证据在哪个版本生效”——
  * 连接证据版本（connection_versions）的物化与复用；
  * 人工审核批次（confirm / reject / adjust_ratio）的追加与版本落位；
  * 按版本解析每个网格的有效连接状态（互斥份额 + 不确定候选 + 被排除候选）。

版本纪律（对应需求）：
  * 候选与审核均为追加式，不原地修改；
  * 人工确认在情景计算途中到达时，只物化为 *后续* 连接版本——既有情景、
    已完成任务与已发布结果都固定在各自版本上，不会被改写；
  * 情景签名引用连接版本号，新版本产生新签名，旧结果物理上不可被覆盖。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Optional

from .evidence import (
    DISPOSITION_EFFECTIVE,
    DISPOSITION_EXCLUDED,
    DISPOSITION_WEAK,
    classify_candidate,
)


# ------------------------------------------------------------- 版本物化 --
def _state_fingerprint(candidates: list[dict], reviews: list[dict]) -> str:
    """候选+审核的内容指纹（不含版本编号，保证版本号变化不影响内容判定）。"""
    payload = {
        "candidates": [
            [
                c["candidate_id"], c["grid_id"], c["node_id"], c["locate_method"],
                float(c["error_radius_m"]), c["allocation_ratio"] and float(c["allocation_ratio"]),
                bool(c["crosses_barrier"]), c["road_class"], c["road_version_id"],
            ]
            for c in candidates
        ],
        "reviews": [
            [r["review_id"], r["candidate_id"], r["action"],
             r["ratio_override"] and float(r["ratio_override"]), r["reviewer"]]
            for r in reviews
        ],
    }
    raw = json.dumps(payload, sort_keys=True, default=str, ensure_ascii=False)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _load_state(conn) -> tuple[list[dict], list[dict]]:
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM grid_connection_candidates ORDER BY candidate_id")
        candidates = cur.fetchall()
        cur.execute("SELECT * FROM connection_reviews ORDER BY review_id")
        reviews = cur.fetchall()
    return candidates, reviews


def materialize_connection_version(conn, note: str = "") -> tuple[dict, bool]:
    """若证据集相对最新版本有变化（含未落位的新行），物化新版本；否则复用最新版本。

    返回 (版本行, 是否新建)。新候选/新审核先以 NULL 版本列插入，物化时统一回填，
    保证“版本 N 的状态 = introduced_version_id <= N 的候选 + connection_version_id <= N 的审核”。
    """
    candidates, reviews = _load_state(conn)
    fingerprint = _state_fingerprint(candidates, reviews)
    has_unversioned = any(c["introduced_version_id"] is None for c in candidates) or any(
        r["connection_version_id"] is None for r in reviews
    )
    with conn.cursor() as cur:
        cur.execute("SELECT * FROM connection_versions ORDER BY version_id DESC LIMIT 1")
        latest = cur.fetchone()
        if latest is not None and latest["fingerprint"] == fingerprint and not has_unversioned:
            return latest, False
        cur.execute(
            "INSERT INTO connection_versions (fingerprint, note) VALUES (%s,%s) RETURNING *",
            (fingerprint, note or "证据集变更自动物化"),
        )
        version = cur.fetchone()
        vid = version["version_id"]
        cur.execute(
            "UPDATE grid_connection_candidates SET introduced_version_id=%s "
            "WHERE introduced_version_id IS NULL",
            (vid,),
        )
        cur.execute(
            "UPDATE connection_reviews SET connection_version_id=%s "
            "WHERE connection_version_id IS NULL",
            (vid,),
        )
    conn.commit()
    return version, True


def get_current_connection_version(conn) -> dict:
    """返回当前证据集对应的版本（必要时物化新版本）。"""
    version, _ = materialize_connection_version(conn)
    return version


# ------------------------------------------------------------- 审核批次 --
def apply_review_batch(
    conn,
    reviews: list[dict],
    *,
    reviewer: str,
    note: str = "",
) -> dict[str, Any]:
    """追加一批人工审核并物化新连接版本。

    每个审核：{candidate_id, action: confirm|reject|adjust_ratio, ratio_override?, note?}。
    审核只追加、不改写历史；已固定旧版本的情景/发布不受影响。
    """
    if not reviews:
        raise ValueError("审核批次为空")
    with conn.transaction():
        with conn.cursor() as cur:
            for r in reviews:
                action = r["action"]
                if action not in ("confirm", "reject", "adjust_ratio"):
                    raise ValueError(f"未知审核动作：{action}")
                cur.execute(
                    "SELECT candidate_id FROM grid_connection_candidates WHERE candidate_id=%s",
                    (r["candidate_id"],),
                )
                if cur.fetchone() is None:
                    raise ValueError(f"候选 {r['candidate_id']} 不存在")
                ratio_override = r.get("ratio_override")
                if action == "adjust_ratio" and ratio_override is None:
                    raise ValueError("adjust_ratio 必须给出 ratio_override")
                cur.execute(
                    """INSERT INTO connection_reviews
                           (candidate_id, action, ratio_override, reviewer, note)
                       VALUES (%s,%s,%s,%s,%s)""",
                    (r["candidate_id"], action, ratio_override, reviewer, r.get("note") or note),
                )
    version, _ = materialize_connection_version(conn, note or f"人工审核批次（{reviewer}）")
    return {"version": version, "applied": len(reviews), "reviewer": reviewer}


# ------------------------------------------------------------- 版本解析 --
def _latest_review_by_candidate(reviews: list[dict]) -> dict[int, dict]:
    latest: dict[int, dict] = {}
    for r in reviews:  # review_id 单调递增，后写覆盖先写
        latest[r["candidate_id"]] = r
    return latest


def resolve_connections(conn, version_id: int) -> dict[str, dict[str, Any]]:
    """按连接证据版本解析每个网格的连接状态。

    返回 {grid_id: {
        shares:    [{candidate_id, node_id, ratio}],   # 证据充分的互斥份额（权重和 ≤ 1）
        ambiguous: [CandidateView...],                 # 弱证据或未配权重候选（仅参与上下界）
        excluded:  [CandidateView...],                 # 被排除候选（留痕含原因）
        candidates:[CandidateView...],                 # 全部候选及其处置（供下钻）
        ratio_sum: float,
        unknown_code: None | 'unknown_no_connector' | 'unknown_no_plausible_candidate',
    }}
    """
    with conn.cursor() as cur:
        cur.execute(
            "SELECT * FROM grid_connection_candidates "
            "WHERE introduced_version_id IS NOT NULL AND introduced_version_id <= %s "
            "ORDER BY candidate_id",
            (version_id,),
        )
        candidates = cur.fetchall()
        cur.execute(
            "SELECT * FROM connection_reviews "
            "WHERE connection_version_id IS NOT NULL AND connection_version_id <= %s "
            "ORDER BY review_id",
            (version_id,),
        )
        reviews = cur.fetchall()

    latest_review = _latest_review_by_candidate(reviews)
    states: dict[str, dict[str, Any]] = {}
    for c in candidates:
        review = latest_review.get(c["candidate_id"])
        review_action = review["action"] if review else None
        disposition, disposition_reason = classify_candidate(
            locate_method=c["locate_method"],
            error_radius_m=float(c["error_radius_m"]),
            crosses_barrier=bool(c["crosses_barrier"]),
            review_action=review_action,
        )
        # 有效权重：审核覆盖优先于候选自带权重
        ratio = c["allocation_ratio"]
        if review_action in ("confirm", "adjust_ratio") and review and review["ratio_override"] is not None:
            ratio = review["ratio_override"]
        view = {
            "candidate_id": c["candidate_id"],
            "grid_id": c["grid_id"],
            "node_id": c["node_id"],
            "locate_method": c["locate_method"],
            "error_radius_m": float(c["error_radius_m"]),
            "distance_to_node_m": float(c["distance_to_node_m"]),
            "allocation_ratio": float(ratio) if ratio is not None else None,
            "road_version_id": c["road_version_id"],
            "road_class": c["road_class"],
            "crosses_barrier": bool(c["crosses_barrier"]),
            "disposition": disposition,
            "disposition_reason": disposition_reason,
            "review_action": review_action,
            "reviewer": review["reviewer"] if review else None,
            "reviewed_at": review["reviewed_at"].isoformat() if review else None,
            "review_note": review["note"] if review else None,
        }
        state = states.setdefault(
            c["grid_id"],
            {"shares": [], "ambiguous": [], "excluded": [], "candidates": [],
             "ratio_sum": 0.0, "unknown_code": None},
        )
        state["candidates"].append(view)
        if disposition == DISPOSITION_EXCLUDED:
            state["excluded"].append(view)
        elif disposition == DISPOSITION_EFFECTIVE and ratio is not None:
            state["shares"].append(
                {"candidate_id": c["candidate_id"], "node_id": c["node_id"],
                 "ratio": float(ratio)}
            )
            state["ratio_sum"] += float(ratio)
        else:
            # 弱证据候选，或证据充分但未配互斥权重：都进入不确定块（上下界）
            state["ambiguous"].append(view)

    for grid_id, state in states.items():
        if not state["candidates"]:
            state["unknown_code"] = "unknown_no_connector"
        elif not state["shares"] and not state["ambiguous"]:
            state["unknown_code"] = "unknown_no_plausible_candidate"
    return states


def disposition_label(disposition: str) -> str:
    return {
        DISPOSITION_EFFECTIVE: "证据充分",
        DISPOSITION_WEAK: "弱证据（仅上下界）",
        DISPOSITION_EXCLUDED: "已排除",
    }[disposition]
