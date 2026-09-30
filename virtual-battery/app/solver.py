"""虚拟电池求解器（纯整数、向后动态规划）。

情景包含 T 个时段（8 <= T <= 48），每段给出整数：
  load[t]  负荷（用电）
  pv[t]    光伏发电
  price[t] 购电单价

电池参数：
  capacity       容量上限（<= 50）
  initial_soc    初始电量
  final_min_soc  终点最低电量（最后一段结束后 soc >= 该值）
  max_charge / max_discharge  每段最大充 / 放电量

逐段守恒，物理规则：
  * 同一段不允许既充又放（动作 a_t：正=充电量，负=放电量，0=静置）；
  * 放电不得超过净负载 net_t = load_t - pv_t（即不能把光伏存进电池后再放空、
    也不能借放电量“售电”），discharge_t <= max(net_t, 0)；
  * 多余光伏可以弃用（curtail），但不能售电（购电量始终非负）；
  * 充放电、购电量、弃光量、电量均为整数单位。

目标按字典序依次为：
  1) 总购电费用 sum(price_t * buy_t) 最小；
  2) 总购电量 sum(buy_t) 最小；
  3) 充放电“动作序列” [a_0, ..., a_{T-1}] 字典序最小
     （a_t = charge_t - discharge_t，范围 [-max_discharge, max_charge]）。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

INF = 10**18


@dataclass(frozen=True)
class Scenario:
    load: tuple[int, ...]
    pv: tuple[int, ...]
    price: tuple[int, ...]
    capacity: int
    initial_soc: int
    final_min_soc: int
    max_charge: int
    max_discharge: int

    @property
    def periods(self) -> int:
        return len(self.load)


@dataclass(frozen=True)
class SegmentEvidence:
    """单段结果与守恒证据。

    能量守恒：soc_start + buy + pv_used == load + soc_end
    其中 pv_used = pv - curtailed；充电来自购电、放电去向负荷，均通过 soc_end 体现。
    """

    t: int
    action: int          # +充电 / -放电 / 0 静置
    charge: int
    discharge: int
    soc_start: int
    soc_end: int
    buy: int
    price: int
    cost: int
    pv: int
    pv_used: int
    curtailed: int
    load: int
    net_load: int        # load - pv（可正可负）
    balance_lhs: int     # soc_start + charge + buy + pv_used
    balance_rhs: int     # load + soc_end


@dataclass(frozen=True)
class SolveResult:
    feasible: bool
    reason: Optional[str]
    total_cost: int
    total_buy: int
    total_curtailed: int
    actions: tuple[int, ...]
    evidence: tuple[SegmentEvidence, ...]


def _legal_actions(sc: Scenario, soc: int, t: int) -> list[int]:
    """返回状态 (soc) 在第 t 段所有合法动作（保持物理可行，弃光由证据端推导）。"""
    net = sc.load[t] - sc.pv[t]
    actions: list[int] = []
    # 放电：a < 0，受放电上限、净负载（不能借光伏/售电）、存量约束
    max_d = min(sc.max_discharge, soc, max(net, 0))
    for a in range(-max_d, 0):
        if soc + a >= 0:
            actions.append(a)
    # 静置
    actions.append(0)
    # 充电：a > 0，受充电上限、容量约束
    max_c = min(sc.max_charge, sc.capacity - soc)
    actions.extend(range(1, max_c + 1))
    return actions


def _flows(sc: Scenario, soc: int, action: int, t: int) -> tuple[int, int, int, int]:
    """给定状态与动作，返回 (soc_end, buy, pv_used, curtailed)。"""
    net = sc.load[t] - sc.pv[t]
    if action < 0:  # 放电段：放电量 <= 净负载，光伏全部被负荷消纳
        discharge = -action
        soc_end = soc - discharge
        buy = max(net - discharge, 0)
        pv_used = min(sc.pv[t], sc.load[t])
    elif action > 0:  # 充电段：净负载先由购电补齐，再购电充入电池
        soc_end = soc + action
        buy = max(net, 0) + action
        pv_used = min(sc.pv[t], sc.load[t])
    else:  # 静置
        soc_end = soc
        buy = max(net, 0)
        pv_used = min(sc.pv[t], sc.load[t])
    curtailed = sc.pv[t] - pv_used
    return soc_end, buy, pv_used, curtailed


def solve(sc: Scenario) -> SolveResult:
    """向后 DP：对每段每个电量状态保留“后缀最优标签” (费用, 购电量)。

    目标层级费用 > 购电量 > 动作序列字典序；在费用与购电量相同的并列项之间，
    字典序最小的完整动作序列由“从第 0 段起每步取最小动作”重构得到，
    因此转移扫描按动作从小到大，保留首个严格更优标签即可。
    """
    T = sc.periods
    C = sc.capacity

    # best[t][soc] = (suffix_cost, suffix_buy)，表示 t..T-1 段可达终点的最优后缀
    best: list[list[Optional[tuple[int, int]]]] = [
        [None] * (C + 1) for _ in range(T + 1)
    ]
    # 终点：soc >= final_min_soc
    for soc in range(sc.final_min_soc, C + 1):
        best[T][soc] = (0, 0)

    for t in range(T - 1, -1, -1):
        price = sc.price[t]
        for soc in range(C + 1):
            tag: Optional[tuple[int, int]] = None
            # 动作升序扫描：相同 (费用, 购电量) 的并列解里首现者对应
            # 最小当前动作，进而给出字典序最小序列。
            for action in _legal_actions(sc, soc, t):
                soc_end, buy, _, _ = _flows(sc, soc, action, t)
                suffix = best[t + 1][soc_end]
                if suffix is None:
                    continue
                cand = (price * buy + suffix[0], buy + suffix[1])
                if tag is None or cand < tag:
                    tag = cand
            best[t][soc] = tag

    if best[0][sc.initial_soc] is None:
        return SolveResult(
            feasible=False,
            reason=(
                "不存在满足终点最低电量的合法充放电计划"
                "（受容量、每段充放电上限与放电不得超过净负载约束）"
            ),
            total_cost=0,
            total_buy=0,
            total_curtailed=0,
            actions=(),
            evidence=(),
        )

    # 前向重构：在保持后缀最优的动作中取最小者
    actions: list[int] = []
    evidence: list[SegmentEvidence] = []
    soc = sc.initial_soc
    total_cost = total_buy = total_curtailed = 0
    for t in range(T):
        chosen: Optional[int] = None
        cur = best[t][soc]
        for action in _legal_actions(sc, soc, t):
            soc_end, buy, _, _ = _flows(sc, soc, action, t)
            suffix = best[t + 1][soc_end]
            if suffix is None:
                continue
            cand = (sc.price[t] * buy + suffix[0], buy + suffix[1])
            if cand == cur:
                chosen = action
                break
        assert chosen is not None
        action = chosen
        soc_start = soc
        soc_end, buy, pv_used, curtailed = _flows(sc, soc, action, t)
        charge = max(action, 0)
        discharge = max(-action, 0)
        cost = sc.price[t] * buy
        net = sc.load[t] - sc.pv[t]
        ev = SegmentEvidence(
            t=t,
            action=action,
            charge=charge,
            discharge=discharge,
            soc_start=soc_start,
            soc_end=soc_end,
            buy=buy,
            price=sc.price[t],
            cost=cost,
            pv=sc.pv[t],
            pv_used=pv_used,
            curtailed=curtailed,
            load=sc.load[t],
            net_load=net,
            balance_lhs=soc_start + buy + pv_used,
            balance_rhs=sc.load[t] + soc_end,
        )
        evidence.append(ev)
        actions.append(action)
        total_cost += cost
        total_buy += buy
        total_curtailed += curtailed
        soc = soc_end

    assert soc >= sc.final_min_soc
    return SolveResult(
        feasible=True,
        reason=None,
        total_cost=total_cost,
        total_buy=total_buy,
        total_curtailed=total_curtailed,
        actions=tuple(actions),
        evidence=tuple(evidence),
    )


def brute_force(sc: Scenario) -> Optional[tuple[int, int, tuple[int, ...]]]:
    """短时段对拍用：递归穷举所有合法动作，返回 (费用, 购电量, 动作序列) 最优值。

    与 DP 完全独立的实现；并列时同样取动作序列字典序最小者。
    """
    T = sc.periods

    def rec(t: int, soc: int) -> Optional[tuple[int, int, tuple[int, ...]]]:
        if t == T:
            if soc >= sc.final_min_soc:
                return (0, 0, ())
            return None
        best_tag: Optional[tuple[int, int, tuple[int, ...]]] = None
        for action in _legal_actions(sc, soc, t):
            _, buy, _, _ = _flows(sc, soc, action, t)
            tail = rec(t + 1, soc + action)
            if tail is None:
                continue
            cand = (
                sc.price[t] * buy + tail[0],
                buy + tail[1],
                (action,) + tail[2],
            )
            # 元组比较天然实现 费用 > 购电量 > 动作序列字典序
            if best_tag is None or cand < best_tag:
                best_tag = cand
        return best_tag

    return rec(0, sc.initial_soc)
