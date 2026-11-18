"""覆盖分析：在一个计算任务内完成全部网格份额的可达判定与去重汇总。

口径（对应需求）——“路径可通”与“人口确实有足够证据接入该路径”分开判定：
- 证据充分的候选连接点按互斥份额切分人口，份额人口 = 网格人口 × 互斥比例，
  按其候选节点的路径可达性划入 **确定可达（covered）** 或 **确定不可达（uncovered）**；
- 弱证据或未配互斥权重的候选不压缩成确定结论：其对应人口整块划入
  **连接不确定（uncertain）**，块内每个候选的路径评估留痕于
  grid_candidate_results；任一候选可达则该块计入覆盖上界，否则不计——
  同一网格人口绝不按候选重复计数；
- 无候选、候选全被排除、比例不闭合、连接节点缺失于路网的部分一律单列 unknown，
  既不默认全覆盖也不默认零需求；
- 汇总恒等式：总人口 = 确定可达 + 确定不可达 + 连接不确定 + 未知。
"""
from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Optional

from psycopg.types.json import Json

from ..engine import (
    GraphSnapshot,
    NodeLabel,
    label_with_shelter_wait,
    shelter_arrival,
    solve,
)
from .connection import resolve_connections
from .snapshot import load_population

_RATIO_EPS = 1e-9


class CoverageEngine:
    def __init__(self, conn, snapshot: GraphSnapshot, task: dict, population_as_of,
                 boundary_id=None, connection_version_id=None):
        self.conn = conn
        self.snapshot = snapshot
        self.task = task
        self.as_of = population_as_of
        self.deadline = task["deadline"]
        self.departure = task["departure_time"]
        self.evidence_cutoff = task["evidence_cutoff_at"]
        self.boundary_id = boundary_id
        self.connection_version_id = task.get("connection_version_id") or connection_version_id
        all_grids = load_population(conn, population_as_of)
        if boundary_id:
            self.grids = {gid: g for gid, g in all_grids.items()
                          if g["boundary_id"] == boundary_id}
        else:
            self.grids = all_grids
        # 连接证据按版本解析（人口资料与连接证据是两条独立的版本线）
        self.connections = (
            resolve_connections(conn, self.connection_version_id)
            if self.connection_version_id is not None
            else {}
        )
        # 不确定块的候选评估留痕（run 期间收集，统一落库）
        self._candidate_evals: list[dict] = []

    # ----------------------------------------------------------- 主流程 --
    def run(self) -> dict[str, Any]:
        label_cache: dict[str, dict[str, NodeLabel]] = {}
        share_rows: list[dict] = []
        edge_deps: set[str] = set()
        node_best: dict[str, NodeLabel] = {}

        for grid_id, grid in sorted(self.grids.items()):
            share_rows.extend(
                self._evaluate_grid(grid_id, grid, label_cache, edge_deps, node_best)
            )

        self._persist_node_arrivals(node_best)
        self._persist_edge_dependencies(edge_deps)
        self._persist_candidate_evals()
        summary = self._persist_shares_and_summary(share_rows)
        return summary

    # ----------------------------------------------------- 标签求解复用 --
    def _labels_for(
        self,
        node_id: str,
        label_cache: dict[str, dict[str, NodeLabel]],
        edge_deps: set[str],
        node_best: dict[str, NodeLabel],
    ) -> dict[str, NodeLabel]:
        labels = label_cache.get(node_id)
        if labels is None:
            labels = solve(self.snapshot, node_id, self.departure, self.deadline)
            label_cache[node_id] = labels
            for lab in labels.values():
                edge_deps.update(lab.edge_path)
                best = node_best.get(lab.node_id)
                if best is None or lab.arrival_time < best.arrival_time:
                    node_best[lab.node_id] = lab
        return labels

    # ----------------------------------------------------- 单网格评估 --
    def _evaluate_grid(
        self,
        grid_id: str,
        grid: dict,
        label_cache: dict[str, dict[str, NodeLabel]],
        edge_deps: set[str],
        node_best: dict[str, NodeLabel],
    ) -> list[dict]:
        base = {
            "task_id": self.task["task_id"],
            "grid_id": grid_id,
            "evidence_cutoff_at": self.evidence_cutoff,
        }
        population = grid["population"]
        state = self.connections.get(grid_id)

        # 未知口径 1：完全没有候选连接点
        if state is None or not state["candidates"]:
            return [
                self._unknown_row(
                    base, grid, 0, population, None,
                    "unknown_no_connector", "网格未登记任何候选连接点，无法定位到路网",
                )
            ]
        # 未知口径 2：证据充分的互斥比例之和 > 1（数据矛盾，整格列为未知）
        if state["ratio_sum"] > 1.0 + _RATIO_EPS:
            return [
                self._unknown_row(
                    base, grid, 0, population, None,
                    "unknown_ratio_over_one",
                    f"证据充分的连接分配比例之和 {state['ratio_sum']:.4f} > 1，分配依据矛盾",
                )
            ]
        # 未知口径 3：候选全部（人工/自动）排除，没有任何可用连接假设
        if state["unknown_code"] == "unknown_no_plausible_candidate":
            return [
                self._unknown_row(
                    base, grid, 0, population, None,
                    "unknown_no_plausible_candidate",
                    "全部候选连接点均被排除（人工排除或低精度质心跨河未确认），无可用接入依据",
                )
            ]

        rows: list[dict] = []
        for idx, share in enumerate(state["shares"]):
            node_id = share["node_id"]
            share_pop = population * share["ratio"]
            row = {
                **base,
                "share_index": idx,
                "connector_node": node_id,
                "share_population": round(share_pop, 3),
                "candidate_id": share["candidate_id"],
            }
            # 未知口径 4：连接节点不在当前路网版本中
            if node_id not in self.snapshot.nodes:
                rows.append(
                    self._unknown_row(
                        row, grid, idx, share_pop, node_id,
                        "unknown_connector_missing",
                        f"连接节点 {node_id} 不存在于截止点路网版本中",
                    )
                )
                continue

            labels = self._labels_for(node_id, label_cache, edge_deps, node_best)
            best_choice = self._best_shelter_for_share(node_id, labels)
            if best_choice is None:
                rows.append(
                    {
                        **row,
                        "reachable": False,
                        "shelter_id": None,
                        "departure_time": self.departure,
                        "arrival_time": None,
                        "total_wait_sec": 0.0,
                        "witness_edge_path": None,
                        "status": "uncovered",
                        "reason": self._unreachable_reason(node_id, labels),
                    }
                )
            else:
                shelter, final_label, shelter_wait = best_choice
                rows.append(
                    {
                        **row,
                        "reachable": True,
                        "shelter_id": shelter.shelter_id,
                        "departure_time": self.departure,
                        "arrival_time": final_label.arrival_time,
                        "total_wait_sec": round(
                            final_label.total_wait_seconds, 3
                        ),
                        "witness_edge_path": Json(list(final_label.edge_path)),
                        "status": "covered",
                        "reason": None,
                    }
                )

        # 残余人口（1 - 互斥比例和）：有候选则连接不确定，无候选则分配依据缺失
        gap = 1.0 - state["ratio_sum"]
        next_index = len(state["shares"])
        if gap > _RATIO_EPS:
            block_pop = population * gap
            if state["ambiguous"]:
                rows.append(
                    self._uncertain_block(
                        base, grid, next_index, block_pop, state["ambiguous"],
                        label_cache, edge_deps, node_best,
                    )
                )
            else:
                rows.append(
                    self._unknown_row(
                        base, grid, next_index, block_pop, None,
                        "unknown_allocation_gap",
                        f"连接分配比例之和 {state['ratio_sum']:.4f} < 1，"
                        f"缺少 {gap:.2%} 份额的分配依据",
                    )
                )
        return rows

    # ------------------------------------------------- 连接不确定块评估 --
    def _uncertain_block(
        self,
        base: dict,
        grid: dict,
        share_index: int,
        block_pop: float,
        ambiguous: list[dict],
        label_cache: dict[str, dict[str, NodeLabel]],
        edge_deps: set[str],
        node_best: dict[str, NodeLabel],
    ) -> dict:
        """弱证据/未配权重候选的整块评估：人口只计一次，候选逐个评估留痕。

        任一候选可达 → 该块计入覆盖上界；全部不可达 → 上界也不计。
        绝不把整格人口重复加入多个安置点。
        """
        grid_id = grid["grid_id"]
        any_reachable = False
        detail: list[str] = []
        for cand in ambiguous:
            node_id = cand["node_id"]
            eval_row = {
                "task_id": self.task["task_id"],
                "grid_id": grid_id,
                "candidate_id": cand["candidate_id"],
                "node_id": node_id,
                "reachable": False,
                "shelter_id": None,
                "arrival_time": None,
                "evidence_sufficient": cand["disposition"] == "effective",
                "exclusion_reason": None,
            }
            if node_id not in self.snapshot.nodes:
                eval_row["exclusion_reason"] = (
                    f"候选节点 {node_id} 不存在于截止点路网版本，未参与路径评估"
                )
                detail.append(f"{node_id}(节点缺失)")
            else:
                labels = self._labels_for(node_id, label_cache, edge_deps, node_best)
                best_choice = self._best_shelter_for_share(node_id, labels)
                if best_choice is not None:
                    shelter, final_label, _ = best_choice
                    eval_row["reachable"] = True
                    eval_row["shelter_id"] = shelter.shelter_id
                    eval_row["arrival_time"] = final_label.arrival_time
                    any_reachable = True
                    detail.append(f"{node_id}(可达{ shelter.shelter_id })")
                else:
                    detail.append(f"{node_id}(不可达)")
            self._candidate_evals.append(eval_row)

        upper_note = "任一候选可达，计入覆盖上界" if any_reachable else "无候选可达，不计入覆盖上界"
        return {
            **base,
            "connector_node": "",
            "share_index": share_index,
            "share_population": round(block_pop, 3),
            "reachable": False,
            "shelter_id": None,
            "departure_time": None,
            "arrival_time": None,
            "total_wait_sec": 0.0,
            "witness_edge_path": None,
            "candidate_id": None,
            "status": "uncertain",
            "reason": (
                f"connection_uncertain: {len(ambiguous)} 个候选连接点证据不足或未配互斥权重"
                f"（{'、'.join(detail)}），人口整块计入连接不确定且只计一次；{upper_note}"
            ),
        }

    def _best_shelter_for_share(
        self, origin: str, labels: dict[str, NodeLabel]
    ) -> Optional[tuple]:
        """在总时限内寻找实际到达时刻最早且当时开放的安置点。"""
        candidates = []
        for shelter in self.snapshot.shelters:
            label = labels.get(shelter.node_id)
            if label is None:
                continue
            node = self.snapshot.nodes.get(shelter.node_id)
            decision = shelter_arrival(shelter, bool(node and node.allow_wait), label, self.deadline)
            if decision is None:
                continue
            arrival, shelter_wait = decision
            if arrival > self.deadline:
                continue  # 等道路恢复/等开放后超出总时限，不计入覆盖
            final_label = label_with_shelter_wait(label, shelter_wait, shelter)
            if final_label.arrival_time > self.deadline:
                continue
            candidates.append((arrival, shelter, final_label, shelter_wait))
        if not candidates:
            return None
        arrival, shelter, final_label, shelter_wait = min(candidates, key=lambda c: c[0])
        return shelter, final_label, shelter_wait

    def _unreachable_reason(self, origin: str, labels: dict[str, NodeLabel]) -> str:
        shelter_nodes = {s.node_id: s for s in self.snapshot.shelters}
        reached = [(n, shelter_nodes[n]) for n in shelter_nodes if n in labels]
        if not reached:
            return "no_shelter_node_within_deadline: 时限内无任何安置点节点可达（含封路/单向边限制）"
        detail = []
        for node_id, shelter in reached:
            if shelter_arrival(shelter, bool(self.snapshot.nodes[node_id].allow_wait),
                               labels[node_id], self.deadline) is None:
                node = self.snapshot.nodes[node_id]
                why = ("节点不允许等待" if not node.allow_wait
                       else "等待至下一开放区间将超出总时限")
                detail.append(f"{shelter.shelter_id}@{node_id} 到达时未开放（{why}）")
        return "shelter_closed_at_arrival: " + "；".join(detail)

    # ------------------------------------------------------------- 落库 --
    def _persist_node_arrivals(self, node_best: dict[str, NodeLabel]) -> None:
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO node_arrivals
                       (task_id, node_id, arrival_time, wait_cost_sec,
                        witness_node_path, witness_edge_path)
                   VALUES (%s,%s,%s,%s,%s,%s)
                   ON CONFLICT (task_id, node_id) DO UPDATE SET
                       arrival_time = LEAST(node_arrivals.arrival_time, EXCLUDED.arrival_time)""",
                [
                    (
                        self.task["task_id"],
                        lab.node_id,
                        lab.arrival_time,
                        round(lab.total_wait_seconds, 3),
                        Json(list(lab.node_path)),
                        Json(list(lab.edge_path)),
                    )
                    for lab in node_best.values()
                ],
            )

    def _persist_edge_dependencies(self, edge_deps: set[str]) -> None:
        if not edge_deps:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO task_edge_dependencies (task_id, edge_id) VALUES (%s,%s) "
                "ON CONFLICT DO NOTHING",
                [(self.task["task_id"], e) for e in sorted(edge_deps)],
            )

    def _persist_candidate_evals(self) -> None:
        if not self._candidate_evals:
            return
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO grid_candidate_results
                       (task_id, grid_id, candidate_id, node_id, reachable,
                        shelter_id, arrival_time, evidence_sufficient, exclusion_reason)
                   VALUES (%(task_id)s,%(grid_id)s,%(candidate_id)s,%(node_id)s,
                           %(reachable)s,%(shelter_id)s,%(arrival_time)s,
                           %(evidence_sufficient)s,%(exclusion_reason)s)
                   ON CONFLICT (task_id, grid_id, candidate_id) DO NOTHING""",
                self._candidate_evals,
            )

    def _persist_shares_and_summary(self, share_rows: list[dict]) -> dict[str, Any]:
        with self.conn.cursor() as cur:
            cur.executemany(
                """INSERT INTO grid_share_results
                       (task_id, grid_id, connector_node, share_index, share_population,
                        reachable, shelter_id, departure_time, arrival_time, total_wait_sec,
                        witness_edge_path, status, reason, candidate_id, evidence_cutoff_at)
                   VALUES (%(task_id)s,%(grid_id)s,%(connector_node)s,%(share_index)s,
                           %(share_population)s,%(reachable)s,%(shelter_id)s,%(departure_time)s,
                           %(arrival_time)s,%(total_wait_sec)s,%(witness_edge_path)s,
                           %(status)s,%(reason)s,%(candidate_id)s,%(evidence_cutoff_at)s)
                   ON CONFLICT (task_id, grid_id, share_index) DO NOTHING""",
                share_rows,
            )

        total = sum(g["population"] for g in self.grids.values())
        # 各桶直接按份额行汇总：每行恰属一类，天然满足人口守恒
        covered_by_grid: dict[str, float] = {}
        uncovered = uncertain = unknown = 0.0
        for r in share_rows:
            pop = r["share_population"]
            if r["status"] == "covered":
                covered_by_grid[r["grid_id"]] = covered_by_grid.get(r["grid_id"], 0.0) + pop
            elif r["status"] == "uncovered":
                uncovered += pop
            elif r["status"] == "uncertain":
                uncertain += pop
            else:
                unknown += pop
        covered = sum(covered_by_grid.values())  # 份额互斥，天然去重
        located = covered + uncovered  # 证据充分、可确定分类的人口

        # 覆盖上界：连接不确定块中任一候选可达的部分计入上界（每块只计一次）
        potentially_by_grid: dict[str, float] = {}
        uncertain_rows = {r["grid_id"]: r for r in share_rows if r["status"] == "uncertain"}
        evals_by_grid: dict[str, list[dict]] = {}
        for ev in self._candidate_evals:
            evals_by_grid.setdefault(ev["grid_id"], []).append(ev)
        for gid, row in uncertain_rows.items():
            if any(ev["reachable"] for ev in evals_by_grid.get(gid, [])):
                potentially_by_grid[gid] = row["share_population"]
        uncertain_potentially_covered = sum(potentially_by_grid.values())
        covered_upper = covered + uncertain_potentially_covered

        summary = {
            "total_population": round(total, 3),
            "located_population": round(located, 3),
            "unknown_population": round(unknown, 3),
            "covered_population": round(covered, 3),
            "uncovered_population": round(uncovered, 3),
            "uncertain_population": round(uncertain, 3),
            "uncertain_potentially_covered": round(uncertain_potentially_covered, 3),
            "covered_upper_population": round(covered_upper, 3),
            "covered_grid_count": len(covered_by_grid),
            "unknown_grid_count": len(
                {r["grid_id"] for r in share_rows if r["status"] == "unknown"}
            ),
            "uncertain_grid_count": len(uncertain_rows),
        }
        with self.conn.cursor() as cur:
            cur.execute(
                """INSERT INTO coverage_summaries
                       (task_id, total_population, located_population, unknown_population,
                        covered_population, uncovered_population, uncertain_population,
                        uncertain_potentially_covered, covered_upper_population,
                        covered_grid_count, unknown_grid_count, uncertain_grid_count)
                   VALUES (%(task_id)s,%(total_population)s,%(located_population)s,
                           %(unknown_population)s,%(covered_population)s,
                           %(uncovered_population)s,%(uncertain_population)s,
                           %(uncertain_potentially_covered)s,%(covered_upper_population)s,
                           %(covered_grid_count)s,%(unknown_grid_count)s,
                           %(uncertain_grid_count)s)
                   ON CONFLICT (task_id) DO UPDATE SET
                       total_population=EXCLUDED.total_population,
                       located_population=EXCLUDED.located_population,
                       unknown_population=EXCLUDED.unknown_population,
                       covered_population=EXCLUDED.covered_population,
                       uncovered_population=EXCLUDED.uncovered_population,
                       uncertain_population=EXCLUDED.uncertain_population,
                       uncertain_potentially_covered=EXCLUDED.uncertain_potentially_covered,
                       covered_upper_population=EXCLUDED.covered_upper_population,
                       covered_grid_count=EXCLUDED.covered_grid_count,
                       unknown_grid_count=EXCLUDED.unknown_grid_count,
                       uncertain_grid_count=EXCLUDED.uncertain_grid_count,
                       computed_at=now()""",
                {**summary, "task_id": self.task["task_id"]},
            )
        return summary

    def _unknown_row(
        self,
        base: dict,
        grid: dict,
        share_index: int,
        share_pop: float,
        node_id: Optional[str],
        reason_code: str,
        reason: str,
    ) -> dict:
        return {
            **base,
            "connector_node": node_id or "",
            "share_index": share_index,
            "share_population": round(share_pop, 3),
            "reachable": False,
            "shelter_id": None,
            "departure_time": None,
            "arrival_time": None,
            "total_wait_sec": 0.0,
            "witness_edge_path": None,
            "candidate_id": None,
            "status": "unknown",
            "reason": f"{reason_code}: {reason}",
        }
