#!/usr/bin/env bash
# 数控刀路禁入区审查服务调用示例
# 用法: bash examples/curl_examples.sh
set -euo pipefail

BASE="${BASE:-http://localhost:8080}"

echo "== 健康检查 =="
curl -s "$BASE/health"; echo

echo "== 1) 登记禁区 1：10x10 mm 方块，Z 闭合范围 [-5, 5] =="
curl -s -X POST "$BASE/zones" \
  -H 'Content-Type: application/json' \
  -d '{
        "zone_no": 1,
        "polygon": [[0,0],[10,0],[10,10],[0,10]],
        "z_min": -5,
        "z_max": 5,
        "description": "基座（毫米）"
      }'; echo

echo "== 2) 查看禁区及版本 =="
curl -s "$BASE/zones/1"; echo

echo "== 3) 提交审查：横穿禁区（应当碰撞，首个碰撞在第 2 行） =="
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 5, 0],
        "tool_radius": 0.0,
        "gcode": "G21 G90 G17\nG1 X15 Y5 Z0\n"
      }'; echo

echo "== 4) 提交审查：从禁区上方经过（无碰撞） =="
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 50, 0],
        "tool_radius": 0.0,
        "gcode": "G1 X15 Y50 Z0\n"
      }'; echo

echo "== 5) 非法指令（G54 坐标系）必须被拒绝，且不产生报告 =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [0,0,0],
        "tool_radius": 0.0,
        "gcode": "G54 G1 X1\n"
      }'

echo "== 6) 登记禁区新版本（几何平移到远处），旧版本保持不变 =="
curl -s -X POST "$BASE/zones/1/versions" \
  -H 'Content-Type: application/json' \
  -d '{
        "polygon": [[100,100],[110,100],[110,110],[100,110]],
        "z_min": -5,
        "z_max": 5
      }'; echo

echo "== 7) 同一刀路按新版本审查（无碰撞） =="
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 2}],
        "start": [-5, 5, 0],
        "tool_radius": 0.0,
        "gcode": "G1 X15 Y5 Z0\n"
      }'; echo

echo "== 8) 旧报告内容不随后续禁区更新改变 =="
curl -s "$BASE/reports/1"; echo

echo "== 9) 可选：启用全程最小水平净间隙计算（include_min_clearance=true） =="
# 先把禁区恢复为版本 1 风格（如已执行第 6 步，可显式引用 v1）；
# 路径 y=11 与方块边 y=10 距离 1mm，刀半径 0.5 => 最小净间隙 0.5mm
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 11, 0],
        "tool_radius": 0.5,
        "gcode": "G1 X15 Y11 Z0\n",
        "include_min_clearance": true
      }'; echo

echo "== 10) 未启用最小净间隙的请求：min_clearance 为 null，旧调用完全兼容 =="
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 11, 0],
        "tool_radius": 0.5,
        "gcode": "G1 X15 Y11 Z0\n"
      }'; echo

echo "== 11) 多刀半径映射（tool_radii）：T2 先于本行运动生效 =="
# 初始刀半径 tool_radius=0，首段停在安全区 (-2,11)（距方块角 sqrt(5)mm）；
# 独立 T2 行换刀 r=2（静止刀位仍安全）；随后 y=11 横穿，最近距离 1mm < 2 => 碰撞，
# 报告固化首碰撞行号 3、刀号 2、半径 2
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 11, 0],
        "tool_radius": 0.0,
        "tool_radii": {"2": 2.0},
        "gcode": "G1 X-2 Y11 Z0\nT2\nG1 X15 Y11 Z0\n",
        "include_min_clearance": true
      }'; echo

echo "== 12) T 可与运动同行（新半径作用于本行运动）；换回小刀后不再碰撞 =="
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 50, 0],
        "tool_radius": 2.0,
        "tool_radii": {"1": 0.5},
        "gcode": "T1 G1 X-5 Y50\nG1 X15 Y11 Z0\n"
      }'; echo

echo "== 13) 多刀模式非法输入一律 422：非整数/负数/同行多个 T/未映射刀号/非法半径 =="
for body in \
  '{"zones":[{"zone_no":1,"version":1}],"start":[0,0,0],"tool_radius":0.0,"gcode":"T1.5\n","tool_radii":{"1":1}}' \
  '{"zones":[{"zone_no":1,"version":1}],"start":[0,0,0],"tool_radius":0.0,"gcode":"T9\n","tool_radii":{"1":1}}' \
  '{"zones":[{"zone_no":1,"version":1}],"start":[0,0,0],"tool_radius":0.0,"gcode":"T1\n","tool_radii":{"-1":1}}' \
  '{"zones":[{"zone_no":1,"version":1}],"start":[0,0,0],"tool_radius":0.0,"gcode":"T1\n","tool_radii":{"1":-0.5}}'; do
  curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/reviews" \
    -H 'Content-Type: application/json' -d "$body"
done

echo "== 14) 多刀执行事件轨迹（execution_trace）：同行 T 与运动分别记录 =="
# 同行 T1 换 r=2 的刀：轨迹依次记录 初始刀位 -> 换刀点（静止，先按新半径检查）
# -> 本行运动；换刀点 (0,11) 距方块边 1mm < 2 => tool_change 事件即首个命中，
# 首碰撞行号 1、刀号 1；轨迹随报告固化，GET /reports/{id} 反复查询一致
curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [0, 11, 0],
        "tool_radius": 0.0,
        "tool_radii": {"1": 2.0},
        "gcode": "T1 G1 X5 Y11 Z0\n"
      }'; echo

echo "== 15) 回看上一次多刀报告的固化轨迹（不受禁区新版本影响） =="
LAST_REPORT=$(curl -s -X POST "$BASE/reviews" \
  -H 'Content-Type: application/json' \
  -d '{
        "zones": [{"zone_no": 1, "version": 1}],
        "start": [-5, 11, 0],
        "tool_radius": 0.0,
        "tool_radii": {"2": 2.0},
        "gcode": "G1 X-2 Y11 Z0\nT2\nG1 X15 Y11 Z0\n"
      }' | sed -n 's/.*"report_id":\([0-9]*\).*/\1/p')
curl -s "$BASE/reports/$LAST_REPORT"; echo

echo "== 16) 批量审查：一次提交 3 项有序任务（同一事务校验，逐项返回报告编号） =="
# 第 0 项单刀碰撞；第 1 项无碰撞；第 2 项多刀+最小净间隙。任务互不影响，
# 报告编号随 index 递增，每项均可 GET /reports/{report_id} 独立查询
curl -s -X POST "$BASE/reviews/batch" \
  -H 'Content-Type: application/json' \
  -d '{
        "batch_id": "batch-demo-0001",
        "tasks": [
          {"zones": [{"zone_no": 1, "version": 1}],
           "start": [-5, 5, 0], "tool_radius": 0.5,
           "gcode": "G1 X15 Y5 Z0\n"},
          {"zones": [{"zone_no": 1, "version": 1}],
           "start": [-5, 50, 0], "tool_radius": 0.0,
           "gcode": "G1 X15 Y50 Z0\n"},
          {"zones": [{"zone_no": 1, "version": 1}],
           "start": [-5, 11, 0], "tool_radius": 0.0,
           "tool_radii": {"2": 2.0}, "include_min_clearance": true,
           "gcode": "G1 X-2 Y11 Z0\nT2\nG1 X15 Y11 Z0\n"}
        ]
      }'; echo

echo "== 17) 幂等重放：相同 batch_id + 相同内容 => 首次逐项结果（不生成新报告） =="
curl -s -X POST "$BASE/reviews/batch" \
  -H 'Content-Type: application/json' \
  -d '{
        "batch_id": "batch-demo-0001",
        "tasks": [
          {"zones": [{"zone_no": 1, "version": 1}],
           "start": [-5, 5, 0], "tool_radius": 0.5,
           "gcode": "G1 X15 Y5 Z0\n"},
          {"zones": [{"zone_no": 1, "version": 1}],
           "start": [-5, 50, 0], "tool_radius": 0.0,
           "gcode": "G1 X15 Y50 Z0\n"},
          {"zones": [{"zone_no": 1, "version": 1}],
           "start": [-5, 11, 0], "tool_radius": 0.0,
           "tool_radii": {"2": 2.0}, "include_min_clearance": true,
           "gcode": "G1 X-2 Y11 Z0\nT2\nG1 X15 Y11 Z0\n"}
        ]
      }'; echo

echo "== 18) 冲突：相同 batch_id + 不同内容 => 409（不生成新报告） =="
curl -s -o /dev/null -w "HTTP %{http_code}\n" -X POST "$BASE/reviews/batch" \
  -H 'Content-Type: application/json' \
  -d '{
        "batch_id": "batch-demo-0001",
        "tasks": [
          {"zones": [{"zone_no": 1, "version": 1}],
           "start": [-5, 6, 0], "tool_radius": 0.5,
           "gcode": "G1 X15 Y6 Z0\n"}
        ]
      }'

echo "== 19) 逐项错误：任一任务非法 => 422 且整批不生成报告（batch_id 不占用） =="
# 第 1 项 G-code 非法、第 2 项禁区不存在：item_errors 逐项列出，整批拒绝
curl -s -X POST "$BASE/reviews/batch" \
  -H 'Content-Type: application/json' \
  -d '{
        "batch_id": "batch-demo-0002",
        "tasks": [
          {"zones": [{"zone_no": 1, "version": 1}],
           "start": [-5, 5, 0], "tool_radius": 0.5,
           "gcode": "G1 X15 Y5 Z0\n"},
          {"zones": [{"zone_no": 1, "version": 1}],
           "start": [0, 0, 0], "tool_radius": 0.0,
           "gcode": "G54 G1 X1\n"},
          {"zones": [{"zone_no": 999, "version": 1}],
           "start": [0, 0, 0], "tool_radius": 0.0,
           "gcode": "G1 X1\n"}
        ]
      }'; echo
