"""API 测试：修订生命周期、并发竞争、旧求解响应失效、对拍一致性。"""
from __future__ import annotations

import threading

import pytest
from fastapi.testclient import TestClient

from app.main import app
from app.solver import Scenario, brute_force
from app.storage import ScenarioStore


@pytest.fixture
def client(monkeypatch) -> TestClient:
    # 每个测试用独立存储，避免互相污染
    monkeypatch.setattr("app.main.store", ScenarioStore())
    return TestClient(app)


def base_scenario(t: int = 8) -> dict:
    return {
        "load": [5] * t,
        "pv": [2] * t,
        "price": [3] * t,
        "capacity": 20,
        "initial_soc": 5,
        "final_min_soc": 5,
        "max_charge": 5,
        "max_discharge": 5,
    }


def _create(client, payload=None) -> dict:
    resp = client.post("/api/scenarios", json=payload or base_scenario())
    assert resp.status_code == 201, resp.text
    return resp.json()


# ---------- 修订生命周期 ----------

def test_create_revision_starts_at_one(client):
    rec = _create(client)
    assert rec["revision"] == 1
    got = client.get(f"/api/scenarios/{rec['id']}").json()
    assert got["revision"] == 1


def test_save_with_correct_revision_increments(client):
    rec = _create(client)
    payload = base_scenario()
    payload["load"][0] = 9
    resp = client.put(
        f"/api/scenarios/{rec['id']}?expected_revision=1", json=payload
    )
    assert resp.status_code == 200, resp.text
    assert resp.json()["revision"] == 2
    # 情景内容确实更新
    got = client.get(f"/api/scenarios/{rec['id']}").json()
    assert got["scenario"]["load"][0] == 9
    assert got["revision"] == 2


def test_stale_revision_is_rejected_with_409(client):
    rec = _create(client)
    sid = rec["id"]
    # 第一次保存成功：1 -> 2
    ok = client.put(f"/api/scenarios/{sid}?expected_revision=1", json=base_scenario())
    assert ok.status_code == 200
    # 仍持修订号 1 的第二次保存必须被拒绝
    stale = client.put(f"/api/scenarios/{sid}?expected_revision=1", json=base_scenario())
    assert stale.status_code == 409
    detail = stale.json()["detail"]
    assert detail["expected_revision"] == 1
    assert detail["current_revision"] == 2
    # 被拒绝的保存不得改变修订号与内容
    assert client.get(f"/api/scenarios/{sid}").json()["revision"] == 2


def test_concurrent_saves_only_one_wins(client):
    """并发保存需用预期修订号拒绝覆盖：同号并发只允许一个成功。"""
    rec = _create(client)
    sid = rec["id"]
    results: list[int] = []
    barrier = threading.Barrier(8)

    def worker(i: int) -> None:
        barrier.wait()  # 尽量让所有线程同时发起
        payload = base_scenario()
        payload["load"][0] = 10 + i
        resp = client.put(
            f"/api/scenarios/{sid}?expected_revision=1", json=payload
        )
        results.append(resp.status_code)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(8)]
    for th in threads:
        th.start()
    for th in threads:
        th.join()

    assert results.count(200) == 1
    assert results.count(409) == 7
    assert client.get(f"/api/scenarios/{sid}").json()["revision"] == 2


def test_chained_revisions_each_step_validated(client):
    rec = _create(client)
    sid = rec["id"]
    for rev in range(1, 5):
        payload = base_scenario()
        payload["final_min_soc"] = rev
        resp = client.put(
            f"/api/scenarios/{sid}?expected_revision={rev}", json=payload
        )
        assert resp.status_code == 200
        assert resp.json()["revision"] == rev + 1
    assert client.get(f"/api/scenarios/{sid}").json()["revision"] == 5


# ---------- 求解与旧响应失效 ----------

def test_solve_returns_evidence_and_balances(client):
    rec = _create(client)
    resp = client.post(
        f"/api/scenarios/{rec['id']}/solve?expected_revision=1"
    )
    assert resp.status_code == 200, resp.text
    data = resp.json()
    assert data["revision"] == 1
    assert data["feasible"]
    assert len(data["evidence"]) == 8
    assert len(data["actions"]) == 8
    for e in data["evidence"]:
        assert not (e["charge"] > 0 and e["discharge"] > 0)
        assert e["discharge"] <= max(e["net_load"], 0)
        assert e["balance_lhs"] == e["balance_rhs"]
        assert e["pv_used"] + e["curtailed"] == e["pv"]
        assert e["buy"] >= 0 and e["curtailed"] >= 0
    assert sum(e["buy"] for e in data["evidence"]) == data["total_buy"]
    assert sum(e["cost"] for e in data["evidence"]) == data["total_cost"]


def test_stale_solve_rejected_after_scenario_edited(client):
    """页面编辑时旧求解响应不得成为新情景的计划：旧修订号求解返回 409。"""
    rec = _create(client)
    sid = rec["id"]
    # 旧页面持修订号 1 先求解成功
    old = client.post(f"/api/scenarios/{sid}/solve?expected_revision=1")
    assert old.status_code == 200
    # 情景被保存为修订号 2
    new_payload = base_scenario()
    new_payload["load"][0] = 99
    saved = client.put(f"/api/scenarios/{sid}?expected_revision=1", json=new_payload)
    assert saved.status_code == 200
    # 持修订号 1 的旧求解请求必须失败，响应中带当前修订号
    stale = client.post(f"/api/scenarios/{sid}/solve?expected_revision=1")
    assert stale.status_code == 409
    assert stale.json()["detail"]["current_revision"] == 2
    # 用新修订号求解得到新计划
    fresh = client.post(f"/api/scenarios/{sid}/solve?expected_revision=2")
    assert fresh.status_code == 200
    assert fresh.json()["revision"] == 2


def test_solve_unknown_scenario_404(client):
    assert client.post("/api/scenarios/nope/solve?expected_revision=1").status_code == 404
    assert client.get("/api/scenarios/nope").status_code == 404
    assert client.put(
        "/api/scenarios/nope?expected_revision=1", json=base_scenario()
    ).status_code == 404


def test_infeasible_scenario_solve_payload(client):
    payload = base_scenario()
    payload["initial_soc"] = 0
    payload["final_min_soc"] = 20
    payload["max_charge"] = 1  # 8 段最多充 8，永远到不了 20
    rec = _create(client, payload)
    resp = client.post(
        f"/api/scenarios/{rec['id']}/solve?expected_revision=1"
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["feasible"] is False
    assert data["reason"]
    assert data["evidence"] == []
    assert data["actions"] == []


# ---------- 输入校验 ----------

@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(load=[1] * 7),            # 少于 8 段
        lambda p: p.update(load=[1] * 49),           # 超过 48 段
        lambda p: p.update(pv=[1] * 7),              # 长度不一致
        lambda p: p.update(capacity=51),             # 容量超 50
        lambda p: p.update(initial_soc=21),          # 初始电量超容量
        lambda p: p.update(final_min_soc=21),        # 终点电量超容量
        lambda p: p.update(max_charge=0),            # 每段充电量为 0
        lambda p: p.update(load=[-1] * 8),           # 负负荷
    ],
)
def test_invalid_inputs_rejected(client, mutate):
    payload = base_scenario()
    mutate(payload)
    # 长度不一致的改动需要同步修正其它序列长度以精确触发目标校验分支
    if len(payload["load"]) != 8 and len(payload["pv"]) == 8:
        pass
    resp = client.post("/api/scenarios", json=payload)
    assert resp.status_code == 422, resp.text


# ---------- API 结果与穷举对拍一致 ----------

def test_api_solve_matches_brute_force(client):
    # 8 段小容量，穷举可行
    payload = {
        "load": [4, 1, 6, 0, 5, 3, 2, 7],
        "pv":   [2, 5, 1, 4, 0, 3, 6, 1],
        "price": [3, 1, 4, 0, 5, 2, 1, 6],
        "capacity": 6,
        "initial_soc": 2,
        "final_min_soc": 3,
        "max_charge": 3,
        "max_discharge": 2,
    }
    rec = _create(client, payload)
    data = client.post(
        f"/api/scenarios/{rec['id']}/solve?expected_revision=1"
    ).json()
    sc = Scenario(
        tuple(payload["load"]), tuple(payload["pv"]), tuple(payload["price"]),
        payload["capacity"], payload["initial_soc"], payload["final_min_soc"],
        payload["max_charge"], payload["max_discharge"],
    )
    bf = brute_force(sc)
    assert bf is not None
    assert (data["total_cost"], data["total_buy"], tuple(data["actions"])) == bf


def test_index_page_served(client):
    resp = client.get("/")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
