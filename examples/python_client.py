"""数控刀路禁入区审查服务 Python 调用示例（仅需 requests 或 httpx）。

运行：
    pip install httpx
    BASE_URL=http://localhost:8080 python examples/python_client.py
"""
from __future__ import annotations

import os

import httpx

BASE = os.environ.get("BASE_URL", "http://localhost:8080")


def main() -> None:
    c = httpx.Client(base_url=BASE, timeout=10.0)

    print("health:", c.get("/health").json())

    # 1. 登记禁区（毫米多边形 + 闭合 Z 范围）
    z = c.post("/zones", json={
        "zone_no": 1,
        "polygon": [[0, 0], [10, 0], [10, 10], [0, 10]],
        "z_min": -5.0,
        "z_max": 5.0,
        "description": "基座",
    })
    z.raise_for_status()
    print("zone created:", z.json())

    # 2. 提交审查：G21 公制 / G90 绝对 / G17 XY，直线横穿禁区
    gcode = "G21 G90 G17\nG1 X-5 Y5 Z0\nG1 X15 Y5 Z0\n"
    review = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5.0, 5.0, 0.0],
        "tool_radius": 0.0,
        "gcode": gcode,
    })
    review.raise_for_status()
    body = review.json()
    print("review:", body["conclusion"],
          "line:", body["collision_line_no"],
          "zone:", body["collision_zone_no"])
    report_id = body["report_id"]

    # 3. 圆弧整圆（同起终点 + I/J 非零）示例，禁区在圆顶附近时会在弧中途碰撞
    arc = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-50.0, -50.0, 0.0],
        "tool_radius": 0.0,
        "gcode": ("G90 G21 G17\n"
                  "G1 X10 Y0 Z0\n"
                  "G3 X10 Y0 I-10 J0\n"),  # 绕 (0,0) 的逆时针整圆
    })
    arc.raise_for_status()
    print("full-circle review:", arc.json()["has_collision"])

    # 4. 非法 G-code：必须 422 拒绝，且不产生报告
    bad = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [0.0, 0.0, 0.0],
        "tool_radius": 0.0,
        "gcode": "G54 G1 X1\n",
    })
    print("invalid gcode status:", bad.status_code, bad.json())

    # 5. 登记新版本（修改不可变 => 新版本），旧报告不变
    c.post("/zones/1/versions", json={
        "polygon": [[100, 100], [110, 100], [110, 110], [100, 110]],
        "z_min": -5.0,
        "z_max": 5.0,
    }).raise_for_status()
    old_report = c.get(f"/reports/{report_id}").json()
    print("immutable old report:", old_report["used_zone_versions"],
          old_report["has_collision"])

    # 6. 可选：启用全程最小水平净间隙（沿用既有审查输入）
    #    路径 y=11 与方块上边 y=10 距离 1mm，刀半径 0.5 => 净间隙 0.5mm
    clr = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5.0, 11.0, 0.0],
        "tool_radius": 0.5,
        "gcode": "G1 X15 Y11 Z0\n",
        "include_min_clearance": True,
    })
    clr.raise_for_status()
    body = clr.json()
    mc = body["min_clearance"]
    print("min clearance:", mc)
    assert mc["min_clearance_mm"] == 0.5
    # 结果随报告固化：GET /reports/{id} 与提交响应一致
    assert c.get(f"/reports/{body['report_id']}").json()["min_clearance"] == mc

    # 7. 未启用时字段为 null（旧请求/旧客户端完全兼容）
    legacy = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5.0, 11.0, 0.0],
        "tool_radius": 0.5,
        "gcode": "G1 X15 Y11 Z0\n",
    }).json()
    assert legacy["min_clearance"] is None
    print("legacy compatible: min_clearance =", legacy["min_clearance"])

    # 8. 多刀半径映射（tool_radii：非负整数刀号 -> 非负有限毫米半径）
    #    初始刀仍用 tool_radius；T2 先于其所在行运动生效，独立 T 行按新半径
    #    检查当前静止刀位；报告固化命中时的原行号、刀号与半径。
    mt = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5.0, 11.0, 0.0],
        "tool_radius": 0.0,
        "tool_radii": {"2": 2.0},
        "gcode": ("G1 X-2 Y11 Z0\n"   # 初始刀 r=0，停在安全区
                  "T2\n"              # 独立 T 行：换刀 r=2（静止刀位仍安全）
                  "G1 X15 Y11 Z0\n"),  # r=2 横穿 y=11（最近 1mm）=> 碰撞
        "include_min_clearance": True,
    })
    mt.raise_for_status()
    body = mt.json()
    print("multi-tool collision:",
          "line", body["collision_line_no"],
          "tool", body["collision_tool_no"],
          "radius", body["collision_tool_radius_mm"])
    assert body["collision_line_no"] == 3
    assert body["collision_tool_no"] == 2
    assert body["collision_tool_radius_mm"] == 2.0
    assert body["min_clearance"]["tool_no"] == 2
    # GET /reports/{id} 与提交响应一致（刀号/半径随报告固化）
    snap = c.get(f"/reports/{body['report_id']}").json()
    assert snap["collision_tool_no"] == 2
    assert snap["min_clearance"]["tool_radius_mm"] == 2.0

    # 9. 不传 tool_radii 时 T 仍被忽略（旧单刀请求完全兼容）
    legacy_t = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5.0, 11.0, 0.0],
        "tool_radius": 0.5,
        "gcode": "T9 M6\nG1 X15 Y11 Z0\n",
    })
    legacy_t.raise_for_status()
    j = legacy_t.json()
    assert j["collision_tool_no"] is None
    assert j["execution_trace"] is None  # 单刀请求无轨迹
    print("legacy T ignored: collision_tool_no =", j["collision_tool_no"])

    # 10. 多刀执行事件轨迹（execution_trace）：按执行顺序记录
    #     初始刀位 -> 每次 T 切换的静止刀位（含同行，先于本行运动）-> 每段运动；
    #     每项含原行号、刀号、半径、起止坐标与命中禁区编号（或空）。
    #     同行 T1 换 r=2：换刀点 (0,11) 距方块边 1mm < 2 => 换刀事件即首个命中。
    tr = c.post("/reviews", json={
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [0.0, 11.0, 0.0],
        "tool_radius": 0.0,
        "tool_radii": {"1": 2.0},
        "gcode": "T1 G1 X5 Y11 Z0\n",
    })
    tr.raise_for_status()
    body = tr.json()
    trace = body["execution_trace"]
    print("execution trace:")
    for e in trace:
        print(" ", e["seq"], e["kind"], "line", e["line_no"],
              "tool", e["tool_no"], "r", e["tool_radius_mm"],
              "hit", e["hit_zone_no"])
    assert [e["kind"] for e in trace] == ["initial", "tool_change", "motion"]
    assert trace[0]["hit_zone_no"] is None          # 初始刀 r=0，距离 1mm 不碰
    assert trace[1]["hit_zone_no"] == 1             # 换刀点先按新半径检查：命中
    assert trace[2]["hit_zone_no"] == 1             # 再检查本行运动：同样命中
    # 首碰撞对应轨迹中首个命中事件
    first_hit = next(e for e in trace if e["hit_zone_no"] is not None)
    assert first_hit["line_no"] == body["collision_line_no"] == 1
    assert first_hit["tool_no"] == body["collision_tool_no"] == 1
    # 轨迹随报告固化：GET /reports/{id} 与提交响应一致，禁区新版本不影响
    snap = c.get(f"/reports/{body['report_id']}").json()
    assert snap["execution_trace"] == trace

    # 11. 批量审查：一次提交带唯一 batch_id 的有序任务列表（1~50 项）。
    #     每项结构与 POST /reviews 相同；同一事务校验整批，全部通过才按输入
    #     顺序生成不可变报告，逐项返回报告编号与完整审查结果。
    batch_body = {
        "batch_id": "batch-demo-0001",
        "tasks": [
            {"zones": [{"zone_no": 1, "version": 1}],
             "start": [-5.0, 5.0, 0.0], "tool_radius": 0.5,
             "gcode": "G1 X15 Y5 Z0\n"},                      # 碰撞
            {"zones": [{"zone_no": 1, "version": 1}],
             "start": [-5.0, 50.0, 0.0], "tool_radius": 0.0,
             "gcode": "G1 X15 Y50 Z0\n"},                     # 无碰撞
            {"zones": [{"zone_no": 1, "version": 1}],
             "start": [-5.0, 11.0, 0.0], "tool_radius": 0.0,
             "tool_radii": {"2": 2.0}, "include_min_clearance": True,
             "gcode": "G1 X-2 Y11 Z0\nT2\nG1 X15 Y11 Z0\n"},  # 多刀碰撞
        ],
    }
    br = c.post("/reviews/batch", json=batch_body)
    br.raise_for_status()
    b = br.json()
    print("batch:", b["batch_id"], "items:", b["item_count"])
    for it in b["items"]:
        print(" ", it["index"], "report", it["report_id"],
              it["conclusion"], "line", it["collision_line_no"])
    assert [it["index"] for it in b["items"]] == [0, 1, 2]
    assert b["items"][0]["has_collision"] is True
    assert b["items"][1]["has_collision"] is False
    assert b["items"][2]["collision_tool_no"] == 2
    # 报告编号随输入顺序递增，且可逐个查询（与单项审查报告同构）
    rids = [it["report_id"] for it in b["items"]]
    assert rids == sorted(rids)
    assert c.get(f"/reports/{rids[0]}").json()["has_collision"] is True

    # 幂等重放：相同 batch_id + 相同内容 => 首次逐项结果（响应完全一致）
    replay = c.post("/reviews/batch", json=batch_body)
    replay.raise_for_status()
    assert replay.json() == b
    print("batch replay identical:", replay.json() == b)

    # 冲突：相同 batch_id + 不同内容 => 409，不生成新报告
    conflict = c.post("/reviews/batch", json={
        "batch_id": "batch-demo-0001",
        "tasks": [batch_body["tasks"][0]],  # 内容不同（任务数变了）
    })
    assert conflict.status_code == 409
    print("batch conflict:", conflict.json()["detail"]["error"])

    # 逐项错误：任一任务非法 => 422，item_errors 逐项列出，整批不生成报告
    invalid = c.post("/reviews/batch", json={
        "batch_id": "batch-demo-0002",
        "tasks": [
            batch_body["tasks"][0],
            {"zones": [{"zone_no": 1, "version": 1}],
             "start": [0.0, 0.0, 0.0], "tool_radius": 0.0,
             "gcode": "G54 G1 X1\n"},                         # 非法 G-code
        ],
    })
    assert invalid.status_code == 422
    detail = invalid.json()["detail"]
    print("batch invalid:", detail["error"], detail["item_errors"])
    assert detail["error"] == "invalid_batch"
    assert detail["item_errors"][0]["index"] == 1
    assert detail["item_errors"][0]["error"] == "invalid_gcode"


if __name__ == "__main__":
    main()
