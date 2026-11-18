import { useCallback, useEffect, useState } from "react";
import { api } from "./api";
import type {
  BarrierRow,
  CandidateView,
  ConnectionReview,
  ConnectionVersion,
  GridConnectionOverview,
} from "./types";

const METHOD_LABEL: Record<string, string> = {
  address_standardization: "地址标准化",
  manual_placement: "人工落点",
  village_centroid: "村级质心",
};

const DISPOSITION_LABEL: Record<string, string> = {
  effective: "证据充分",
  weak: "弱证据（仅上下界）",
  excluded: "已排除",
};

const ROAD_CLASS_LABEL: Record<string, string> = {
  dead_end: "断头路",
  local: "普通路",
  arterial: "主干/分流",
};

export default function ConnectionsView({
  pushToast,
}: {
  pushToast: (t: string, k?: "info" | "error") => void;
}) {
  const [current, setCurrent] = useState<ConnectionVersion | null>(null);
  const [versions, setVersions] = useState<ConnectionVersion[]>([]);
  const [reviews, setReviews] = useState<ConnectionReview[]>([]);
  const [grids, setGrids] = useState<GridConnectionOverview[]>([]);
  const [barriers, setBarriers] = useState<BarrierRow[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [candidates, setCandidates] = useState<CandidateView[]>([]);
  const [ratioSum, setRatioSum] = useState(0);
  const [reviewer, setReviewer] = useState("analyst-ui");
  const [ingest, setIngest] = useState({
    node_id: "",
    locate_method: "manual_placement",
    error_radius_m: 25,
    geom_x: 0,
    geom_y: 0,
    allocation_ratio: "" as string,
  });

  const reload = useCallback(async () => {
    const [v, g, b] = await Promise.all([
      api.connectionVersion(),
      api.connectionGrids(),
      api.barriers(),
    ]);
    setCurrent(v.current);
    setVersions(v.versions);
    setReviews(v.reviews);
    setGrids(g.grids);
    setBarriers(b.barriers);
  }, []);

  useEffect(() => {
    reload().catch(() => undefined);
  }, [reload]);

  const selectGrid = useCallback(async (gridId: string) => {
    setSelected(gridId);
    const d = await api.connectionCandidates(gridId);
    setCandidates(d.candidates);
    setRatioSum(d.ratio_sum);
  }, []);

  const doReview = async (
    candidateId: number,
    action: string,
    ratioOverride?: number
  ) => {
    const note = window.prompt(
      `审核意见（${action === "confirm" ? "确认接入" : action === "reject" ? "排除候选" : "调整权重"}，必填）`
    );
    if (note === null) return;
    try {
      const r = await api.reviewCandidates({
        reviews: [
          {
            candidate_id: candidateId,
            action,
            ratio_override: ratioOverride ?? null,
            note: note || undefined,
          },
        ],
        reviewer,
        note: "治理台审核",
      });
      pushToast(
        `审核已入账：形成连接版本 v${r.version.version_id}（既有情景与发布不受影响）`
      );
      await reload();
      if (selected) await selectGrid(selected);
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  const doIngest = async () => {
    if (!selected || !ingest.node_id) {
      pushToast("请先选择网格并填写候选节点", "error");
      return;
    }
    try {
      const r = await api.ingestCandidate({
        grid_id: selected,
        node_id: ingest.node_id,
        locate_method: ingest.locate_method,
        error_radius_m: Number(ingest.error_radius_m),
        geom_x: Number(ingest.geom_x),
        geom_y: Number(ingest.geom_y),
        allocation_ratio: ingest.allocation_ratio
          ? Number(ingest.allocation_ratio)
          : null,
        source_title: "治理台手工登记",
      });
      pushToast(
        `候选已接入（跨河=${r.candidate.crosses_barrier ? "是" : "否"}，道路等级=${r.candidate.road_class ?? "?"}），形成版本 v${r.version.version_id}`
      );
      await reload();
      if (selected) await selectGrid(selected);
    } catch (e) {
      pushToast(String(e), "error");
    }
  };

  return (
    <div className="full-page">
      <div className="card">
        <h3>连接证据治理：候选连接点 · 定位方法 · 误差 · 道路版本 · 人工确认</h3>
        <div className="small muted">
          覆盖计算把“路径可通”与“人口确实有足够证据接入该路径”分开：证据充分的候选按互斥份额形成
          <b>确定可达/确定不可达</b>；弱证据或未配权重候选只形成<b>连接不确定</b>的上、下界，
          同一网格人口绝不重复计数。人工确认只形成后续连接版本，不改写已发布情景。
          {current && (
            <>
              <br />
              当前连接版本：<b>v{current.version_id}</b>（指纹 {current.fingerprint}，
              {current.created_at.replace("T", " ").slice(0, 19)}）
            </>
          )}
        </div>
      </div>

      <div className="card scroll-x">
        <h3>网格连接状态（当前版本）</h3>
        <table>
          <thead>
            <tr>
              <th>网格</th><th>候选数</th><th>证据充分</th><th>弱证据</th>
              <th>已排除</th><th>互斥比例和</th><th>状态</th>
            </tr>
          </thead>
          <tbody>
            {grids.map((g) => (
              <tr
                key={g.grid_id}
                onClick={() => selectGrid(g.grid_id)}
                style={{
                  cursor: "pointer",
                  background: selected === g.grid_id ? "var(--panel-2)" : undefined,
                }}
              >
                <td className="mono">{g.grid_id}<div className="small muted">{g.name}</div></td>
                <td>{g.candidate_count}</td>
                <td className="good">{g.effective_count}</td>
                <td style={{ color: "var(--uncertain)" }}>{g.weak_count}</td>
                <td className="warn">{g.excluded_count}</td>
                <td className={g.ratio_sum > 1 ? "warn" : ""}>{g.ratio_sum.toFixed(2)}</td>
                <td className="small">
                  {g.unknown_code
                    ? <span className="pill unknown">{g.unknown_code}</span>
                    : g.weak_count > 0 || g.ratio_sum < 1
                      ? <span className="pill uncertain">含不确定</span>
                      : <span className="pill covered">确定</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {selected && (
        <div className="card scroll-x">
          <h3>候选连接点明细：{selected}（互斥比例和 {ratioSum.toFixed(2)}）</h3>
          <table>
            <thead>
              <tr>
                <th>#</th><th>节点</th><th>定位方法</th><th>误差(m)</th><th>吸附距离(m)</th>
                <th>道路等级</th><th>道路版本</th><th>跨河</th><th>权重</th><th>处置</th>
                <th>审核留痕</th><th>操作</th>
              </tr>
            </thead>
            <tbody>
              {candidates.map((c) => (
                <tr key={c.candidate_id}>
                  <td className="mono">{c.candidate_id}</td>
                  <td className="mono">{c.node_id}</td>
                  <td>{METHOD_LABEL[c.locate_method] ?? c.locate_method}</td>
                  <td>{c.error_radius_m}</td>
                  <td>{c.distance_to_node_m.toFixed(0)}</td>
                  <td>{c.road_class ? ROAD_CLASS_LABEL[c.road_class] : "—"}</td>
                  <td>{c.road_version_id ? `v${c.road_version_id}` : "—"}</td>
                  <td>{c.crosses_barrier ? <span className="warn">是</span> : "否"}</td>
                  <td>{c.allocation_ratio != null ? c.allocation_ratio.toFixed(2) : "未配"}</td>
                  <td>
                    <span className={`pill ${c.disposition}`}>
                      {DISPOSITION_LABEL[c.disposition]}
                    </span>
                    {c.disposition_reason && (
                      <div className="small muted">{c.disposition_reason}</div>
                    )}
                  </td>
                  <td className="small">
                    {c.review_action
                      ? `${c.review_action} by ${c.reviewer}：${c.review_note ?? ""}`
                      : "—"}
                  </td>
                  <td>
                    <button
                      className="btn secondary"
                      style={{ padding: "2px 8px", marginRight: 4 }}
                      onClick={() => {
                        const ratio = window.prompt(
                          "确认接入的互斥权重（0-1，留空则不配权重）",
                          c.allocation_ratio != null ? String(c.allocation_ratio) : "1"
                        );
                        if (ratio === null) return;
                        doReview(c.candidate_id, "confirm", ratio ? Number(ratio) : undefined);
                      }}
                    >
                      确认
                    </button>
                    <button
                      className="btn secondary"
                      style={{ padding: "2px 8px" }}
                      onClick={() => doReview(c.candidate_id, "reject")}
                    >
                      排除
                    </button>
                  </td>
                </tr>
              ))}
            </tbody>
          </table>

          <h3 style={{ marginTop: 16 }}>接入新候选（形成后续连接版本）</h3>
          <div className="row" style={{ flexWrap: "wrap", gap: 8 }}>
            <label className="field"><span>候选节点</span>
              <input value={ingest.node_id} style={{ width: 90 }}
                onChange={(e) => setIngest({ ...ingest, node_id: e.target.value })} />
            </label>
            <label className="field"><span>定位方法</span>
              <select value={ingest.locate_method}
                onChange={(e) => setIngest({ ...ingest, locate_method: e.target.value })}>
                <option value="manual_placement">人工落点</option>
                <option value="address_standardization">地址标准化</option>
                <option value="village_centroid">村级质心</option>
              </select>
            </label>
            <label className="field"><span>误差半径(m)</span>
              <input type="number" style={{ width: 90 }} value={ingest.error_radius_m}
                onChange={(e) => setIngest({ ...ingest, error_radius_m: Number(e.target.value) })} />
            </label>
            <label className="field"><span>原始定位 X</span>
              <input type="number" style={{ width: 100 }} value={ingest.geom_x}
                onChange={(e) => setIngest({ ...ingest, geom_x: Number(e.target.value) })} />
            </label>
            <label className="field"><span>原始定位 Y</span>
              <input type="number" style={{ width: 100 }} value={ingest.geom_y}
                onChange={(e) => setIngest({ ...ingest, geom_y: Number(e.target.value) })} />
            </label>
            <label className="field"><span>互斥权重(可空)</span>
              <input style={{ width: 90 }} value={ingest.allocation_ratio}
                onChange={(e) => setIngest({ ...ingest, allocation_ratio: e.target.value })} />
            </label>
            <label className="field"><span>审核人</span>
              <input style={{ width: 110 }} value={reviewer}
                onChange={(e) => setReviewer(e.target.value)} />
            </label>
            <button className="btn" onClick={doIngest}>接入候选</button>
          </div>
        </div>
      )}

      <div className="card scroll-x">
        <h3>空间阻隔（河流）与版本/审核留痕</h3>
        <div className="small muted" style={{ marginBottom: 8 }}>
          {barriers.map((b) => (
            <span key={b.barrier_id} className="path-chip">
              {b.barrier_id} {b.name}（{b.geometry.length} 个折点）
            </span>
          ))}
        </div>
        <table>
          <thead>
            <tr><th>版本</th><th>指纹</th><th>说明</th><th>物化时间</th></tr>
          </thead>
          <tbody>
            {versions.map((v) => (
              <tr key={v.version_id}>
                <td className="mono">v{v.version_id}</td>
                <td className="mono small">{v.fingerprint}</td>
                <td className="small">{v.note}</td>
                <td className="small">{v.created_at.replace("T", " ").slice(0, 19)}</td>
              </tr>
            ))}
          </tbody>
        </table>
        <table style={{ marginTop: 10 }}>
          <thead>
            <tr><th>审核#</th><th>候选</th><th>动作</th><th>权重覆盖</th><th>审核人</th><th>意见</th><th>版本</th><th>时间</th></tr>
          </thead>
          <tbody>
            {reviews.map((r) => (
              <tr key={r.review_id}>
                <td className="mono">{r.review_id}</td>
                <td className="mono">{r.candidate_id}</td>
                <td><span className={`pill ${r.action === "reject" ? "excluded" : "effective"}`}>{r.action}</span></td>
                <td>{r.ratio_override ?? "—"}</td>
                <td>{r.reviewer}</td>
                <td className="small">{r.note ?? "—"}</td>
                <td className="mono">v{r.connection_version_id}</td>
                <td className="small">{r.reviewed_at.replace("T", " ").slice(0, 19)}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}
