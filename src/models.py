"""Pydantic contracts for the structured library tools (plan Step 3).

RoutePlan v1.0: pure route geometry in full-res pixel coords (origin
top-left), renderer-agnostic. No meters, no seconds, no scale (per D4).

`point` / `polyline` are [x, y] arrays, never {x, y} objects.
"""

from typing import Literal, Optional
from pydantic import BaseModel, Field

SCHEMA_VERSION = "1.0"

StepKind = Literal[
    "start", "door", "corridor_entry", "corridor_exit", "transit", "end",
]
Direction = Literal["up", "down"]


class Place(BaseModel):
    id: str
    name: str
    type: str
    floor: str
    point: list[int]
    call_number: Optional[str] = None
    other_floors: list[str] = Field(default_factory=list)


class RouteStep(BaseModel):
    kind: StepKind
    point: list[float]
    place_id: Optional[str] = None
    label: str
    instruction: str


class TransitInfo(BaseModel):
    id: str
    name: str
    from_floor: str
    to_floor: str
    direction: Direction
    accessible: bool


class RouteLeg(BaseModel):
    floor: str
    map_file: str
    map_size: list[int]
    polyline: list[list[float]]
    steps: list[RouteStep]
    transit: Optional[TransitInfo] = None


class RouteTotals(BaseModel):
    distance_px: float
    floor_changes: int
    step_free: bool
    transits_used: list[str]


class RoutePlan(BaseModel):
    schema_version: str = SCHEMA_VERSION
    plan_id: str
    generated_at: str
    origin: Place
    destination: Place
    totals: RouteTotals
    legs: list[RouteLeg]
    warnings: list[str] = Field(default_factory=list)


class LocationMatch(BaseModel):
    place: Place
    exact: bool
    score: float


class LocationSearchResult(BaseModel):
    query: str
    matches: list[LocationMatch]
    total_matches: int
    exact_matches: int


class Alert(BaseModel):
    floor: Optional[str] = None
    area: str
    reason: str
    until: Optional[str] = None


class LibraryStatus(BaseModel):
    source: str
    closures: list[Alert] = Field(default_factory=list)
    updated_at: str
