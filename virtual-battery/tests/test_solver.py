"""求解器测试：短时段穷举所有合法动作与 DP 对拍。"""
from __future__ import annotations

import random

from app.solver import Scenario, brute_force, solve


def _random_scenario(rng: random.Random, max_t: int = 7) -> Scenario:
    T = rng.randint(1, max_t)
    cap = rng.randint(2, 8)
    return Scenario(
        load=tuple(rng.randint(0, 6) for _ in range(T)),
        pv=tuple(rng.randint(0, 6) for _ in range(T)),
        price=tuple(rng.randint(0, 5) for _ in range(T)),
        capacity=cap,
        initial_soc=rng.randint(0, cap),
        final_min_soc=rng.randint(0, cap),
        max_charge=rng.randint(1, cap),
        max_discharge=rng.randint(1, cap),
    )


def _assert_evidence_valid(sc: Scenario, r) -> None:
    soc = sc.initial_soc
    for e in r.evidence:
        assert e.soc_start == soc
        # 逐段守恒：soc_start + buy + pv_used == load + soc_end
        assert e.balance_lhs == e.balance_rhs
        # 同段不既充又放；动作编码一致
        assert not (e.charge > 0 and e.discharge > 0)
        assert e.action == e.charge - e.discharge
        assert e.charge <= sc.max_charge
        assert e.discharge <= sc.max_discharge
        # 放电不得超过净负载；不能售电
        assert e.discharge <= max(e.net_load, 0)
        assert e.buy >= 0
        # 光伏账目：消纳 + 弃用 = 发电；弃光非负
        assert e.pv_used + e.curtailed == e.pv
        assert e.pv_used >= 0 and e.curtailed >= 0
        # 电量边界
        assert 0 <= e.soc_end <= sc.capacity
        assert e.soc_end == soc + e.action
        assert e.cost == e.buy * e.price
        soc = e.soc_end
    assert soc >= sc.final_min_soc
    assert sum(e.cost for e in r.evidence) == r.total_cost
    assert sum(e.buy for e in r.evidence) == r.total_buy
    assert sum(e.curtailed for e in r.evidence) == r.total_curtailed


def test_dp_matches_brute_force_random():
    rng = random.Random(20260930)
    for n in range(400):
        sc = _random_scenario(rng)
        r = solve(sc)
        bf = brute_force(sc)
        if bf is None:
            assert not r.feasible, n
            continue
        assert r.feasible, n
        # 三级目标：费用 -> 购电量 -> 动作序列字典序
        assert (r.total_cost, r.total_buy, r.actions) == bf, n
        _assert_evidence_valid(sc, r)


def test_small_handbuilt_case():
    # 2 段（对拍器支持任意短序列）：低价充电、高价放电应被选中
    sc = Scenario(
        load=(5, 5),
        pv=(0, 0),
        price=(1, 10),
        capacity=5,
        initial_soc=0,
        final_min_soc=0,
        max_charge=5,
        max_discharge=5,
    )
    r = solve(sc)
    bf = brute_force(sc)
    assert r.feasible
    assert (r.total_cost, r.total_buy, r.actions) == bf
    # 第 1 段充 5（买 10），第 2 段放 5（买 0）：费用 10
    assert r.actions == (5, -5)
    assert r.total_cost == 10
    assert r.total_buy == 10


def test_curtailment_evidence():
    # 光伏远超负荷且电池已满：必须弃光，购电为 0
    sc = Scenario(
        load=(2, 2),
        pv=(9, 9),
        price=(3, 3),
        capacity=4,
        initial_soc=4,
        final_min_soc=4,
        max_charge=2,
        max_discharge=2,
    )
    r = solve(sc)
    assert r.feasible
    assert r.total_buy == 0
    assert r.total_cost == 0
    assert all(e.curtailed == 7 for e in r.evidence)
    assert all(e.buy == 0 for e in r.evidence)


def test_no_selling_even_with_pv_surplus():
    # 放电受净负载限制：光伏富余段不能靠放电“售电”
    sc = Scenario(
        load=(1, 1),
        pv=(5, 5),
        price=(1, 1),
        capacity=4,
        initial_soc=4,
        final_min_soc=4,
        max_charge=4,
        max_discharge=4,
    )
    r = solve(sc)
    assert r.feasible
    for e in r.evidence:
        assert e.discharge == 0
        assert e.buy == 0


def test_infeasible_final_soc():
    # 终点要求高电量，但没有足够充电窗口
    sc = Scenario(
        load=(0,),
        pv=(0,),
        price=(1,),
        capacity=5,
        initial_soc=0,
        final_min_soc=4,
        max_charge=2,
        max_discharge=2,
    )
    r = solve(sc)
    assert not r.feasible
    assert r.reason
    assert brute_force(sc) is None


def test_lexicographic_tie_prefers_smaller_action():
    # 价格全 0 时费用恒 0；购电量目标之外的并列由动作序列字典序裁决。
    # 构造静置即满足终点的情景：任意充放并列（不花费用），字典序最小应为全 0。
    sc = Scenario(
        load=(3, 3),
        pv=(3, 3),
        price=(0, 0),
        capacity=5,
        initial_soc=2,
        final_min_soc=2,
        max_charge=3,
        max_discharge=3,
    )
    r = solve(sc)
    assert r.feasible
    assert r.actions == (0, 0)
    bf = brute_force(sc)
    assert bf[2] == (0, 0)
    assert (r.total_cost, r.total_buy, r.actions) == bf
