"""地址/空间证据接入：候选连接点的空间质检与确定性分级。

职责边界：本模块只负责“证据长什么样”——
  * 计算候选连接点的空间质检标记（原始定位点→候选节点是否跨越河流阻隔、
    接入点在该道路版本中的道路等级、直线距离）；
  * 按确定性规则给出候选的证据分级（effective / weak / auto-excluded）。

不做路径计算（引擎职责），不做版本管理（connection 模块职责），
不做网格聚合（coverage 模块职责）。所有规则确定、可复算、可解释：

  分级规则（仅针对未审核候选；人工确认/排除优先于一切自动规则）：
  1. 村级质心 且 跨越河流阻隔 → 自动排除（低精度质心跨河不得默认接入最近道路）；
  2. 村级质心、误差半径 > ERROR_THRESHOLD_M、或跨越河流阻隔 之一 → 弱证据（仅参与上下界）；
  3. 其余（地址标准化/人工落点且误差可控且不跨河）→ 证据充分。
"""
from __future__ import annotations

import math
from typing import Any, Optional

# 误差半径阈值：超过该值的定位结果不足以直接支撑“确定”结论
ERROR_THRESHOLD_M = 200.0

STRONG_METHODS = {"address_standardization", "manual_placement"}

# 候选处置类别
DISPOSITION_EFFECTIVE = "effective"    # 证据充分：可形成互斥确定份额
DISPOSITION_WEAK = "weak"              # 证据不足：仅参与连接不确定上下界
DISPOSITION_EXCLUDED = "excluded"      # 被排除：不参与任何计算（留痕可查）


# ------------------------------------------------------------- 几何质检 --
def _segments_intersect(
    p1: tuple[float, float],
    p2: tuple[float, float],
    p3: tuple[float, float],
    p4: tuple[float, float],
) -> bool:
    """二维线段 p1p2 与 p3p4 是否相交（含共线重叠）。"""

    def cross(o: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
        return (a[0] - o[0]) * (b[1] - o[1]) - (a[1] - o[1]) * (b[0] - o[0])

    d1 = cross(p3, p4, p1)
    d2 = cross(p3, p4, p2)
    d3 = cross(p1, p2, p3)
    d4 = cross(p1, p2, p4)
    if ((d1 > 0 and d2 < 0) or (d1 < 0 and d2 > 0)) and (
        (d3 > 0 and d4 < 0) or (d3 < 0 and d4 > 0)
    ):
        return True
    # 端点共线接触也算相交（质心正好压在河岸线上时同样不可默认）
    def on_segment(a: tuple[float, float], b: tuple[float, float], p: tuple[float, float]) -> bool:
        return (
            min(a[0], b[0]) - 1e-9 <= p[0] <= max(a[0], b[0]) + 1e-9
            and min(a[1], b[1]) - 1e-9 <= p[1] <= max(a[1], b[1]) + 1e-9
        )

    if abs(d1) < 1e-9 and on_segment(p3, p4, p1):
        return True
    if abs(d2) < 1e-9 and on_segment(p3, p4, p2):
        return True
    if abs(d3) < 1e-9 and on_segment(p1, p2, p3):
        return True
    if abs(d4) < 1e-9 and on_segment(p1, p2, p4):
        return True
    return False


def crosses_any_barrier(
    raw_point: tuple[float, float],
    node_point: tuple[float, float],
    barriers: list[dict],
) -> bool:
    """原始定位点→候选节点的直线是否与任一阻隔折线相交。"""
    for barrier in barriers:
        coords = barrier["geometry"]
        for i in range(len(coords) - 1):
            a = (float(coords[i][0]), float(coords[i][1]))
            b = (float(coords[i + 1][0]), float(coords[i + 1][1]))
            if _segments_intersect(raw_point, node_point, a, b):
                return True
    return False


def road_class_for_node(out_degree: Optional[int]) -> Optional[str]:
    """按该道路版本中节点出度判定接入道路等级（确定性规则，可复算）。

    0 = dead_end（断头路：无法继续驶出）；1 = local；≥2 = arterial（主干/分流）。
    """
    if out_degree is None:
        return None
    if out_degree == 0:
        return "dead_end"
    if out_degree == 1:
        return "local"
    return "arterial"


# ------------------------------------------------------------- 证据分级 --
def classify_candidate(
    *,
    locate_method: str,
    error_radius_m: float,
    crosses_barrier: bool,
    review_action: Optional[str],
) -> tuple[str, Optional[str]]:
    """返回 (处置类别, 排除/降级原因)。人工审核结论优先于自动规则。"""
    if review_action == "reject":
        return DISPOSITION_EXCLUDED, "人工排除：审核确认该候选不作为接入依据"
    if review_action == "confirm":
        return DISPOSITION_EFFECTIVE, None
    # 未审核（pending）候选的自动规则
    if locate_method == "village_centroid" and crosses_barrier:
        return (
            DISPOSITION_EXCLUDED,
            "低精度村级质心跨越河流阻隔，未经人工确认不得默认接入（禁止按最近直线距离默认）",
        )
    weak_reasons = []
    if locate_method == "village_centroid":
        weak_reasons.append("低精度村级质心")
    if error_radius_m > ERROR_THRESHOLD_M:
        weak_reasons.append(f"误差半径 {error_radius_m:.0f}m 超过阈值 {ERROR_THRESHOLD_M:.0f}m")
    if crosses_barrier:
        weak_reasons.append("原始定位点与候选节点间跨越河流阻隔")
    if weak_reasons:
        return DISPOSITION_WEAK, "；".join(weak_reasons) + "，未经人工确认仅参与上下界"
    return DISPOSITION_EFFECTIVE, None


# ------------------------------------------------------------- 候选接入 --
def load_barriers(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute("SELECT barrier_id, name, geometry FROM spatial_barriers ORDER BY barrier_id")
        return cur.fetchall()


def _node_out_degree(conn, road_version_id: Optional[int], node_id: str) -> Optional[int]:
    """候选所依据道路版本中该节点的出度；节点不在版本中返回 None。"""
    if road_version_id is None:
        return None
    with conn.cursor() as cur:
        cur.execute("SELECT cutoff_at FROM road_network_versions WHERE version_id=%s", (road_version_id,))
        row = cur.fetchone()
        if row is None:
            return None
        cutoff = row["cutoff_at"]
        cur.execute(
            """SELECT count(*) AS n FROM road_edge_revisions
               WHERE from_node=%s AND valid_from <= %s AND (valid_to IS NULL OR valid_to > %s)""",
            (node_id, cutoff, cutoff),
        )
        return int(cur.fetchone()["n"])


def _latest_road_version_id(conn) -> Optional[int]:
    with conn.cursor() as cur:
        cur.execute("SELECT version_id FROM road_network_versions ORDER BY version_id DESC LIMIT 1")
        row = cur.fetchone()
        return row["version_id"] if row else None


def ingest_candidate(
    conn,
    *,
    grid_id: str,
    node_id: str,
    locate_method: str,
    error_radius_m: float,
    geom_x: float,
    geom_y: float,
    allocation_ratio: Optional[float] = None,
    road_version_id: Optional[int] = None,
    source_doc_id: Optional[int] = None,
) -> dict[str, Any]:
    """接入一个候选连接点：计算空间质检标记并落库（不物化版本，由调用方统一物化）。

    道路版本缺省取当前最新路网版本；道路等级按该版本出度计算。
    """
    if locate_method not in ("address_standardization", "manual_placement", "village_centroid"):
        raise ValueError(f"未知定位方法：{locate_method}")
    with conn.cursor() as cur:
        cur.execute("SELECT geom_x, geom_y FROM road_nodes WHERE node_id=%s", (node_id,))
        node = cur.fetchone()
        if node is None:
            raise ValueError(f"候选节点 {node_id} 不存在于路网节点台账")
        cur.execute("SELECT grid_id FROM pop_grids WHERE grid_id=%s", (grid_id,))
        if cur.fetchone() is None:
            raise ValueError(f"网格 {grid_id} 不存在")

    if road_version_id is None:
        road_version_id = _latest_road_version_id(conn)

    node_xy = (float(node["geom_x"]), float(node["geom_y"]))
    raw_xy = (float(geom_x), float(geom_y))
    distance = math.dist(raw_xy, node_xy)
    barriers = load_barriers(conn)
    crosses = crosses_any_barrier(raw_xy, node_xy, barriers)
    out_degree = _node_out_degree(conn, road_version_id, node_id)
    road_class = road_class_for_node(out_degree)

    with conn.cursor() as cur:
        cur.execute(
            """INSERT INTO grid_connection_candidates
                   (grid_id, node_id, locate_method, error_radius_m, geom_x, geom_y,
                    distance_to_node_m, allocation_ratio, road_version_id, road_class,
                    crosses_barrier, source_doc_id)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (
                grid_id, node_id, locate_method, float(error_radius_m),
                float(geom_x), float(geom_y), round(distance, 3),
                allocation_ratio, road_version_id, road_class, crosses, source_doc_id,
            ),
        )
        row = cur.fetchone()
    return row
