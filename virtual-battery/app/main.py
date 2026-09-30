"""FastAPI 求解服务：情景修订存储 + 虚拟电池求解 + Vue 页面。"""
from __future__ import annotations

from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from .models import (
    SaveResponse,
    ScenarioInput,
    ScenarioRecord,
    SegmentEvidenceOut,
    SolveResponse,
)
from .solver import Scenario, solve
from .storage import NotFound, RevisionConflict, ScenarioStore

app = FastAPI(title="虚拟电池情景求解服务", version="1.0.0")
store = ScenarioStore()

STATIC_DIR = Path(__file__).resolve().parent.parent / "static"


def _to_solver_model(sc: ScenarioInput) -> Scenario:
    return Scenario(
        load=tuple(sc.load),
        pv=tuple(sc.pv),
        price=tuple(sc.price),
        capacity=sc.capacity,
        initial_soc=sc.initial_soc,
        final_min_soc=sc.final_min_soc,
        max_charge=sc.max_charge,
        max_discharge=sc.max_discharge,
    )


@app.post("/api/scenarios", response_model=ScenarioRecord, status_code=201)
def create_scenario(scenario: ScenarioInput) -> dict:
    """新建情景，修订号从 1 开始。"""
    return store.create(scenario)


@app.get("/api/scenarios/{scenario_id}", response_model=ScenarioRecord)
def get_scenario(scenario_id: str) -> dict:
    try:
        return store.get(scenario_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="情景不存在")


@app.put("/api/scenarios/{scenario_id}", response_model=SaveResponse)
def save_scenario(
    scenario_id: str,
    scenario: ScenarioInput,
    expected_revision: int = Query(..., description="客户端持有的预期修订号"),
) -> dict:
    """保存情景修订。expected_revision 不匹配时返回 409 拒绝覆盖。"""
    try:
        return store.save(scenario_id, scenario, expected_revision)
    except NotFound:
        raise HTTPException(status_code=404, detail="情景不存在")
    except RevisionConflict as exc:
        current = store.get(scenario_id)
        raise HTTPException(
            status_code=409,
            detail={
                "message": str(exc),
                "expected_revision": expected_revision,
                "current_revision": current["revision"],
            },
        )


@app.post("/api/scenarios/{scenario_id}/solve", response_model=SolveResponse)
def solve_scenario(
    scenario_id: str,
    expected_revision: int = Query(..., description="只求解该修订号；过期则 409"),
) -> SolveResponse:
    """对指定修订号求解。情景被别人改过（修订号前进）则旧请求得到 409，

    避免旧求解响应被误当成新情景的计划。
    """
    try:
        record = store.get(scenario_id)
    except NotFound:
        raise HTTPException(status_code=404, detail="情景不存在")
    if expected_revision != record["revision"]:
        raise HTTPException(
            status_code=409,
            detail={
                "message": (
                    f"求解基于过期修订号：预期 {expected_revision}，"
                    f"当前 {record['revision']}；请按最新情景重新求解"
                ),
                "expected_revision": expected_revision,
                "current_revision": record["revision"],
            },
        )

    result = solve(_to_solver_model(record["scenario"]))
    return SolveResponse(
        scenario_id=scenario_id,
        revision=record["revision"],
        feasible=result.feasible,
        reason=result.reason,
        total_cost=result.total_cost,
        total_buy=result.total_buy,
        total_curtailed=result.total_curtailed,
        actions=list(result.actions),
        evidence=[SegmentEvidenceOut(**e.__dict__) for e in result.evidence],
    )


@app.get("/")
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
