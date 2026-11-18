import type {
  ArrivalRow,
  BarrierRow,
  CacheRow,
  CandidateResultRow,
  CandidateView,
  Comparison,
  ConnectionReview,
  ConnectionVersion,
  ConnectorRow,
  EdgeRevision,
  GridConnectionOverview,
  GridRow,
  NodeRow,
  OpenWindow,
  RoadEventRow,
  ScenarioCreatePayload,
  ScenarioListRow,
  ScenarioResult,
  ShareRow,
  ShelterRow,
  SourceDoc,
  Summary,
  TaskRow,
} from "./types";

const BASE = "/api";

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const resp = await fetch(`${BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...init,
  });
  if (!resp.ok) {
    const text = await resp.text();
    throw new Error(`${resp.status} ${resp.statusText}: ${text}`);
  }
  return resp.json() as Promise<T>;
}

export const api = {
  health: () => request<{ status: string }>("/health"),

  createScenario: (payload: ScenarioCreatePayload) =>
    request<ScenarioResult>("/scenarios", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  listScenarios: () =>
    request<{ scenarios: ScenarioListRow[] }>("/scenarios"),

  taskSummary: (taskId: string) =>
    request<{ task: TaskRow; summary: Summary | null }>(
      `/tasks/${taskId}/summary`
    ),

  taskGrids: (taskId: string) =>
    request<{ shares: ShareRow[] }>(`/tasks/${taskId}/grids`),

  taskArrivals: (taskId: string) =>
    request<{ arrivals: ArrivalRow[] }>(`/tasks/${taskId}/arrivals`),

  taskWitness: (taskId: string, gridId: string) =>
    request<{
      grid_id: string;
      shares: ShareRow[];
      connector_arrivals: Array<ArrivalRow | null>;
      task_edge_dependencies: Array<{ task_id: string; edge_id: string }>;
      candidate_results: CandidateResultRow[];
      connection_version_id: number | null;
      candidates: CandidateView[];
      bounds_impact: {
        grid_uncertain_population: number;
        counts_toward_upper_bound: boolean;
        task_covered_lower: number;
        task_covered_upper: number;
        task_uncertain_population: number;
        task_uncertain_potentially_covered: number;
      } | null;
    }>(`/tasks/${taskId}/witness?grid_id=${encodeURIComponent(gridId)}`),

  releaseViewer: (taskId: string, viewerId: string) =>
    request<{
      task_id: string;
      viewer_id: string;
      remaining_viewers: number;
      cancelled: boolean;
      note: string;
    }>(`/tasks/${taskId}/release`, {
      method: "POST",
      body: JSON.stringify({ viewer_id: viewerId }),
    }),

  listTasks: () =>
    request<{ tasks: TaskRow[]; cache_index: CacheRow[] }>("/tasks"),

  documents: () =>
    request<{ documents: SourceDoc[] }>("/sources/documents"),

  boundaries: () =>
    request<{
      boundaries: Array<{
        boundary_id: string;
        name: string;
        geometry: number[][];
      }>;
    }>("/sources/boundaries"),

  nodes: () => request<{ nodes: NodeRow[] }>("/sources/nodes"),

  edges: () => request<{ edge_revisions: EdgeRevision[] }>("/sources/edges"),

  events: (includeAudit = false) =>
    request<{
      events: RoadEventRow[];
      audits: Array<Record<string, unknown>>;
    }>(`/sources/events?include_audit=${includeAudit}`),

  shelters: () =>
    request<{ shelters: ShelterRow[]; open_windows: OpenWindow[] }>(
      "/sources/shelters"
    ),

  grids: (asOf: string) =>
    request<{
      grids: GridRow[];
      connectors: ConnectorRow[];
      connection_version_id: number;
    }>(`/sources/grids?as_of=${encodeURIComponent(asOf)}`),

  connectionVersion: () =>
    request<{
      current: ConnectionVersion;
      versions: ConnectionVersion[];
      reviews: ConnectionReview[];
    }>("/connections/version"),

  connectionGrids: () =>
    request<{ version_id: number; grids: GridConnectionOverview[] }>(
      "/connections/grids"
    ),

  connectionCandidates: (gridId: string) =>
    request<{
      version_id: number;
      grid_id: string;
      candidates: CandidateView[];
      shares: Array<{ candidate_id: number; node_id: string; ratio: number }>;
      ratio_sum: number;
      unknown_code: string | null;
    }>(`/connections/candidates?grid_id=${encodeURIComponent(gridId)}`),

  ingestCandidate: (payload: Record<string, unknown>) =>
    request<{ candidate: CandidateView; version: ConnectionVersion }>(
      "/connections/candidates",
      { method: "POST", body: JSON.stringify(payload) }
    ),

  reviewCandidates: (payload: {
    reviews: Array<{
      candidate_id: number;
      action: string;
      ratio_override?: number | null;
      note?: string;
    }>;
    reviewer: string;
    note?: string;
  }) =>
    request<{ version: ConnectionVersion; applied: number; reviewer: string }>(
      "/connections/reviews",
      { method: "POST", body: JSON.stringify(payload) }
    ),

  barriers: () => request<{ barriers: BarrierRow[] }>("/connections/barriers"),

  compare: (taskA: string, taskB: string) =>
    request<Comparison>("/comparisons", {
      method: "POST",
      body: JSON.stringify({ task_a_id: taskA, task_b_id: taskB }),
    }),

  publish: (taskA: string, taskB: string, title: string) =>
    request<{
      publication: { publication_id: string; [k: string]: unknown };
      comparison: Comparison;
      input_manifest: Record<string, unknown>;
    }>("/publications", {
      method: "POST",
      body: JSON.stringify({
        task_a_id: taskA,
        task_b_id: taskB,
        title,
        created_by: "analyst-ui",
      }),
    }),

  publications: () =>
    request<{ publications: Record<string, unknown>[] }>(
      "/publications"
    ),

  createEvent: (payload: Record<string, unknown>) =>
    request<{
      event_id: string;
      invalidation: {
        edge_id: string;
        reason: string;
        affected_task_ids: string[];
        recovery_candidate_task_ids: string[];
        staled_total: number;
      };
    }>("/events", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  invalidate: (edgeId: string, reason: string) =>
    request<Record<string, unknown>>("/cache/invalidate", {
      method: "POST",
      body: JSON.stringify({ edge_id: edgeId, reason }),
    }),

  precompute: (payload: Record<string, unknown>) =>
    request<{ precomputed: ScenarioResult[] }>("/cache/precompute", {
      method: "POST",
      body: JSON.stringify(payload),
    }),

  rebuild: (newCutoff: string) =>
    request<Record<string, unknown>>("/cache/rebuild", {
      method: "POST",
      body: JSON.stringify({ new_cutoff: newCutoff }),
    }),

  cacheIndex: () =>
    request<{ cache_index: CacheRow[] }>("/cache/index"),

  networkVersions: () =>
    request<{
      versions: Array<{
        version_id: number;
        cutoff_at: string;
        edge_fingerprint: string;
        event_fingerprint: string;
        note: string | null;
      }>;
    }>("/network-versions"),

  reseed: () => request("/admin/reseed", { method: "POST" }),

  applyC2: () => request("/admin/apply-c2-revision", { method: "POST" }),
};
