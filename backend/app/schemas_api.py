"""请求/响应模型。"""
from __future__ import annotations

from datetime import date, datetime
from typing import Optional

from pydantic import BaseModel, Field


class ScenarioCreate(BaseModel):
    name: str = "临时情景"
    population_as_of: date
    departure_time: datetime
    max_trip_minutes: int = Field(gt=0, le=600)
    road_cutoff: datetime
    boundary_id: Optional[str] = None
    viewer_id: str = "analyst-ui"
    note: Optional[str] = None


class ViewerRelease(BaseModel):
    viewer_id: str


class CompareRequest(BaseModel):
    task_a_id: str
    task_b_id: str


class PublishRequest(BaseModel):
    task_a_id: str
    task_b_id: str
    title: str
    created_by: str = "analyst"


class PrecomputeRequest(BaseModel):
    population_as_of: date
    base_day: datetime
    cutoff: datetime
    deadline_minutes: int = 60
    offsets_minutes: list[int] = [0, 30, 60, 90, 120]
    boundary_id: Optional[str] = None


class InvalidateRequest(BaseModel):
    edge_id: str
    reason: str = "道路事件修订"


class RebuildRequest(BaseModel):
    new_cutoff: datetime


class EventCreate(BaseModel):
    """临时封路/限速：立即参与后续即时计算。"""

    event_id: str
    event_type: str  # closure | speed_limit
    edge_id: str
    effective_from: datetime
    effective_to: Optional[datetime] = None
    speed_limit_kmh: Optional[float] = None
    source_title: str = "临时上报事件"
    raw_text: str = ""


class CandidateIngest(BaseModel):
    """候选连接点接入：登记后立即物化新连接版本（此前情景不受影响）。"""

    grid_id: str
    node_id: str
    locate_method: str  # address_standardization | manual_placement | village_centroid
    error_radius_m: float = Field(ge=0)
    geom_x: float
    geom_y: float
    allocation_ratio: Optional[float] = Field(default=None, gt=0, le=1)
    source_title: str = "连接证据登记"
    raw_text: str = ""


class ReviewItem(BaseModel):
    candidate_id: int
    action: str  # confirm | reject | adjust_ratio
    ratio_override: Optional[float] = Field(default=None, gt=0, le=1)
    note: Optional[str] = None


class ReviewBatchCreate(BaseModel):
    """人工审核批次：整批物化为一个后续连接版本，不改写已发布情景。"""

    reviews: list[ReviewItem]
    reviewer: str = "analyst-ui"
    note: str = ""
