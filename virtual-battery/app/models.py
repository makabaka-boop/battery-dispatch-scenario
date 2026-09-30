"""情景与求解响应的 Pydantic 模型（字段校验在保存时完成）。"""
from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator

MIN_PERIODS = 8
MAX_PERIODS = 48
MAX_CAPACITY = 50


class ScenarioInput(BaseModel):
    """一份情景：T 个时段的整数序列与电池参数。"""

    load: list[int] = Field(description="逐段用电（负荷），整数、非负")
    pv: list[int] = Field(description="逐段光伏发电，整数、非负")
    price: list[int] = Field(description="逐段购电单价，整数、非负")
    capacity: int = Field(ge=1, le=MAX_CAPACITY, description="电池容量上限（<=50）")
    initial_soc: int = Field(ge=0, description="初始电量")
    final_min_soc: int = Field(ge=0, description="终点最低电量")
    max_charge: int = Field(ge=1, description="每段最大充电量")
    max_discharge: int = Field(ge=1, description="每段最大放电量")

    @model_validator(mode="after")
    def _validate(self) -> "ScenarioInput":
        if not (MIN_PERIODS <= len(self.load) <= MAX_PERIODS):
            raise ValueError(
                f"时段数必须在 {MIN_PERIODS}~{MAX_PERIODS} 之间，当前为 {len(self.load)}"
            )
        if len(self.pv) != len(self.load) or len(self.price) != len(self.load):
            raise ValueError("load / pv / price 三个序列长度必须一致")
        if any(x < 0 for x in self.load):
            raise ValueError("用电量必须非负")
        if any(x < 0 for x in self.pv):
            raise ValueError("光伏发电量必须非负")
        if any(x < 0 for x in self.price):
            raise ValueError("购电单价必须非负")
        if self.initial_soc > self.capacity:
            raise ValueError("初始电量不能超过容量")
        if self.final_min_soc > self.capacity:
            raise ValueError("终点最低电量不能超过容量")
        if self.max_charge > self.capacity:
            raise ValueError("每段最大充电量不能超过容量")
        if self.max_discharge > self.capacity:
            raise ValueError("每段最大放电量不能超过容量")
        return self


class ScenarioRecord(BaseModel):
    """带修订号的情景记录。"""

    id: str
    revision: int
    scenario: ScenarioInput
    created_at: str
    updated_at: str


class SegmentEvidenceOut(BaseModel):
    t: int
    action: int = Field(description="充放电动作：正=充电，负=放电，0=静置")
    charge: int
    discharge: int
    soc_start: int
    soc_end: int
    buy: int = Field(description="本段购电量")
    price: int
    cost: int
    pv: int
    pv_used: int = Field(description="被消纳的光伏（负荷直接使用）")
    curtailed: int = Field(description="弃光量（多余光伏，可弃不可售）")
    load: int
    net_load: int
    balance_lhs: int
    balance_rhs: int


class SolveResponse(BaseModel):
    scenario_id: str
    revision: int = Field(description="本计划对应的情景修订号；编辑后该计划即过期")
    feasible: bool
    reason: Optional[str] = None
    total_cost: int
    total_buy: int
    total_curtailed: int
    actions: list[int]
    evidence: list[SegmentEvidenceOut]


class SaveResponse(BaseModel):
    id: str
    revision: int
    scenario: ScenarioInput
    created_at: str
    updated_at: str
