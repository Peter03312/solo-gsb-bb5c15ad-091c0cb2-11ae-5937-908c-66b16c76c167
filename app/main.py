"""FastAPI 应用：数控刀路禁入区审查服务。"""
from __future__ import annotations

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from sqlalchemy import func, inspect, select, text
from sqlalchemy.orm import Session

from .collision import (Zone, build_execution_trace, check_path,
                        initial_collision, minimum_clearance,
                        minimum_clearance_program)
from .database import Base, engine, get_db
from .gcode import GCodeError, parse_gcode
from .geometry import GeometryError, normalize_polygon, validate_z_range
from . import models
from .models import Report, ZoneVersion
from .schemas import (MinClearanceOut, ReviewRequest, ReviewResponse,
                      TraceEventOut, ZoneCreate, ZoneDetailOut, ZoneOut,
                      ZoneVersionCreate, ZoneVersionOut)

app = FastAPI(
    title="数控刀路禁入区审查服务",
    version="1.3.0",
    description="登记毫米制二维多边形禁区（含闭合 Z 高度范围、不可变版本），"
                "对 G-code 刀路做连续碰撞审查并出具不可变报告；"
                "可选计算全程最小水平净间隙（include_min_clearance），"
                "可选多刀半径映射（tool_radii：刀号到毫米半径），"
                "多刀模式随报告固化可回看的执行事件轨迹（execution_trace）。",
)


@app.on_event("startup")
def on_startup() -> None:
    # 容器内由应用直接完成数据库初始化（表结构创建）
    Base.metadata.create_all(bind=engine)
    # 旧库 reports 表缺少后续版本新增列：幂等补齐，保证旧请求/旧报告仍可读写
    _ensure_report_columns(engine)


def _ensure_report_columns(db_engine) -> None:
    """对旧数据库幂等添加 reports 表后续版本新增列。

    PostgreSQL 支持 BOOLEAN/JSON 原生类型；SQLite 对列类型仅按名称亲和处理，
    同名 DDL 同样可用。
    """
    inspector = inspect(db_engine)
    if "reports" not in inspector.get_table_names():
        return
    existing = {c["name"] for c in inspector.get_columns("reports")}
    is_pg = db_engine.dialect.name == "postgresql"
    with db_engine.begin() as conn:
        if "min_clearance_enabled" not in existing:
            default = "FALSE" if is_pg else "0"
            conn.execute(text(
                "ALTER TABLE reports ADD COLUMN "
                f"min_clearance_enabled BOOLEAN NOT NULL DEFAULT {default}"))
        if "min_clearance" not in existing:
            conn.execute(text(
                "ALTER TABLE reports ADD COLUMN min_clearance JSON"))
        # v1.2：多刀模式首碰撞命中刀号/半径固化列（旧报告为 NULL）
        if "collision_tool_no" not in existing:
            conn.execute(text(
                "ALTER TABLE reports ADD COLUMN collision_tool_no INTEGER"))
        if "collision_tool_radius" not in existing:
            conn.execute(text(
                "ALTER TABLE reports ADD COLUMN collision_tool_radius FLOAT"))
        # v1.3：多刀模式执行事件轨迹固化列（单刀请求与旧报告为 NULL）
        if "execution_trace" not in existing:
            conn.execute(text(
                "ALTER TABLE reports ADD COLUMN execution_trace JSON"))


@app.exception_handler(GeometryError)
async def geometry_error_handler(request: Request, exc: GeometryError):  # noqa: ARG001
    return JSONResponse(status_code=422,
                        content={"error": "invalid_geometry", "detail": str(exc)})


# ---------- 禁区登记 ----------

@app.post("/zones", response_model=ZoneOut, status_code=201,
          summary="登记新禁区（生成版本 1）")
def create_zone(payload: ZoneCreate, db: Session = Depends(get_db)) -> ZoneOut:
    polygon = normalize_polygon(payload.polygon)
    validate_z_range(payload.z_min, payload.z_max)

    if payload.zone_no is not None:
        exists = db.scalar(select(models.Zone).where(
            models.Zone.zone_no == payload.zone_no))
        if exists:
            raise HTTPException(status_code=409,
                                detail=f"禁区编号 {payload.zone_no} 已存在")
        zone_no = payload.zone_no
    else:
        max_no = db.scalar(select(func.max(models.Zone.zone_no))) or 0
        zone_no = max_no + 1

    zone = models.Zone(zone_no=zone_no, description=payload.description)
    db.add(zone)
    db.flush()
    db.add(ZoneVersion(zone_id=zone.id, version=1, polygon=[list(p) for p in polygon],
                       z_min=payload.z_min, z_max=payload.z_max))
    db.commit()
    db.refresh(zone)
    return ZoneOut(zone_id=zone.id, zone_no=zone.zone_no,
                   description=zone.description, current_version=1,
                   created_at=zone.created_at)


@app.get("/zones", response_model=list[ZoneOut], summary="列出全部禁区")
def list_zones(db: Session = Depends(get_db)) -> list[ZoneOut]:
    zones = db.scalars(select(models.Zone).order_by(models.Zone.zone_no)).all()
    out: list[ZoneOut] = []
    for z in zones:
        cur = db.scalar(select(func.max(ZoneVersion.version)).where(
            ZoneVersion.zone_id == z.id))
        out.append(ZoneOut(zone_id=z.id, zone_no=z.zone_no,
                           description=z.description, current_version=cur or 0,
                           created_at=z.created_at))
    return out


@app.get("/zones/{zone_no}", response_model=ZoneDetailOut,
         summary="查询禁区及其全部不可变版本")
def get_zone(zone_no: int, db: Session = Depends(get_db)) -> ZoneDetailOut:
    zone = db.scalar(select(models.Zone).where(models.Zone.zone_no == zone_no))
    if zone is None:
        raise HTTPException(status_code=404, detail=f"禁区 {zone_no} 不存在")
    versions = db.scalars(
        select(ZoneVersion).where(ZoneVersion.zone_id == zone.id)
        .order_by(ZoneVersion.version)).all()
    return ZoneDetailOut(
        zone_id=zone.id, zone_no=zone.zone_no, description=zone.description,
        current_version=versions[-1].version if versions else 0,
        created_at=zone.created_at,
        versions=[ZoneVersionOut(
            version_id=v.id, version=v.version,
            polygon=[tuple(p) for p in v.polygon],
            z_min=v.z_min, z_max=v.z_max, created_at=v.created_at)
            for v in versions])


@app.post("/zones/{zone_no}/versions", response_model=ZoneVersionOut,
          status_code=201, summary="为禁区登记不可变新版本（修改不改旧版本）")
def add_zone_version(zone_no: int, payload: ZoneVersionCreate,
                     db: Session = Depends(get_db)) -> ZoneVersionOut:
    zone = db.scalar(select(models.Zone).where(models.Zone.zone_no == zone_no))
    if zone is None:
        raise HTTPException(status_code=404, detail=f"禁区 {zone_no} 不存在")

    polygon = normalize_polygon(payload.polygon)
    validate_z_range(payload.z_min, payload.z_max)

    # 行锁保证并发下版本号单调
    db.scalar(
        select(models.Zone).where(models.Zone.id == zone.id).with_for_update())
    latest = db.scalar(select(func.max(ZoneVersion.version)).where(
        ZoneVersion.zone_id == zone.id)) or 0
    ver = ZoneVersion(zone_id=zone.id, version=latest + 1,
                      polygon=[list(p) for p in polygon],
                      z_min=payload.z_min, z_max=payload.z_max)
    db.add(ver)
    db.commit()
    db.refresh(ver)
    return ZoneVersionOut(version_id=ver.id, version=ver.version,
                          polygon=[tuple(p) for p in ver.polygon],
                          z_min=ver.z_min, z_max=ver.z_max,
                          created_at=ver.created_at)


# ---------- 审查 ----------

def _load_zones(db: Session, refs) -> tuple[list[Zone], dict[str, int], dict[int, int]]:
    zones: list[Zone] = []
    used: dict[str, int] = {}
    version_ids: dict[int, int] = {}
    seen: set[int] = set()
    for ref in refs:
        if ref.zone_no in seen:
            raise HTTPException(status_code=422,
                                detail=f"禁区 {ref.zone_no} 在请求中重复")
        seen.add(ref.zone_no)
        zone = db.scalar(select(models.Zone).where(
            models.Zone.zone_no == ref.zone_no))
        if zone is None:
            raise HTTPException(status_code=404,
                                detail=f"禁区 {ref.zone_no} 不存在")
        zv = db.scalar(select(ZoneVersion).where(
            ZoneVersion.zone_id == zone.id,
            ZoneVersion.version == ref.version))
        if zv is None:
            raise HTTPException(
                status_code=404,
                detail=f"禁区 {ref.zone_no} 不存在版本 {ref.version}")
        polygon = normalize_polygon(zv.polygon)  # 防御性再校验
        zones.append(Zone(ref.zone_no, polygon, zv.z_min, zv.z_max))
        used[str(ref.zone_no)] = ref.version
        version_ids[ref.zone_no] = zv.id
    zones.sort(key=lambda z: z.zone_no)
    return zones, used, version_ids


@app.post("/reviews", response_model=ReviewResponse,
          summary="提交刀路审查并生成不可变报告")
def review(payload: ReviewRequest,
           db: Session = Depends(get_db)) -> ReviewResponse:
    if payload.tool_radius < 0:
        raise HTTPException(status_code=422, detail="刀具半径不得为负")

    # 先加载并固化所用禁区版本；任何非法输入都在写报告之前拒绝
    zones, used_versions, _ = _load_zones(db, payload.zones)

    tool_map = payload.tool_radii
    multi_tool = tool_map is not None

    # 解析 G-code；语法/语义错误（含多刀模式下非法/未映射 T）明确拒绝，不留报告
    try:
        if multi_tool:
            parsed = parse_gcode(
                payload.gcode, tuple(payload.start),  # type: ignore[arg-type]
                tool_radius_map=tool_map,
                initial_tool_radius=float(payload.tool_radius))
        else:
            parsed = parse_gcode(
                payload.gcode, tuple(payload.start))  # type: ignore[arg-type]
    except GCodeError as exc:
        raise HTTPException(status_code=422, detail={
            "error": "invalid_gcode",
            "line_no": exc.line_no,
            "message": str(exc),
        })

    summary = {
        "gcode_lines": parsed.line_count,
        "nonblank_lines": parsed.nonblank_count,
        "motion_count": len(parsed.motions),
        "start_position_mm": list(payload.start),
        "tool_radius_mm": payload.tool_radius,
        "final_position_mm": list(parsed.final_position),
        "gcode_sha256": __sha(payload.gcode),
        "include_min_clearance": bool(payload.include_min_clearance),
    }
    if multi_tool:
        # 固化本次多刀配置：刀号（字符串键，JSON 友好）-> 毫米半径
        summary["tool_map_enabled"] = True
        summary["tool_radii_mm"] = {str(k): v for k, v in tool_map.items()}

    trace_payload: list[dict] | None = None
    if multi_tool:
        # 多刀模式：构建执行事件轨迹（初始刀位 -> 各 T 切换静止刀位 ->
        # 各段运动，同行 T 先于本行运动），首碰撞即轨迹中首个命中事件；
        # 轨迹随报告固化，不因禁区新版本改变
        trace = build_execution_trace(
            parsed.motions, parsed.tool_events, zones,
            tuple(payload.start),  # type: ignore[arg-type]
            float(payload.tool_radius))
        collision = next((ev.hit for ev in trace if ev.hit is not None), None)
        min_clr = minimum_clearance_program(
            parsed.motions, parsed.tool_events, zones,
            tuple(payload.start),  # type: ignore[arg-type]
            float(payload.tool_radius)) \
            if payload.include_min_clearance else None
        trace_payload = [{
            "seq": ev.seq,
            "kind": ev.kind,
            "line_no": ev.line_no,
            "tool_no": ev.tool_no,
            "tool_radius_mm": ev.tool_radius,
            "start_mm": [ev.start[0], ev.start[1], ev.start[2]],
            "end_mm": [ev.end[0], ev.end[1], ev.end[2]],
            "hit_zone_no": ev.hit.zone_no if ev.hit is not None else None,
        } for ev in trace]
    else:
        # 单刀模式：旧口径（初始刀位仅在无运动碰撞时补判）保持兼容
        collision = check_path(parsed.motions, zones, payload.tool_radius)
        min_clr = minimum_clearance(parsed.motions, zones, payload.tool_radius,
                                    tuple(payload.start)) \
            if payload.include_min_clearance else None

    line_no: int | None = None
    zone_no: int | None = None
    collision_tool_no: int | None = None
    collision_tool_radius: float | None = None

    if collision is not None:
        line_no = collision.line_no
        zone_no = collision.zone_no
        if multi_tool:
            collision_tool_no = collision.tool_no
            collision_tool_radius = collision.tool_radius
    elif not multi_tool:
        # 无运动段碰撞时检查初始刀位点；有运动段时归属首个运动行号
        static_zone = initial_collision(tuple(payload.start),  # type: ignore[arg-type]
                                        zones, payload.tool_radius)
        if static_zone is not None:
            zone_no = static_zone
            line_no = parsed.motions[0].line_no if parsed.motions else None

    has_coll = zone_no is not None

    if has_coll:
        conclusion = "collision"
        message = (f"发现碰撞：G-code 第 {line_no} 行与禁区编号 {zone_no} 冲突") \
            if line_no is not None else \
            f"初始刀位点位于禁区编号 {zone_no} 范围内"
    else:
        conclusion = "no_collision"
        message = "刀路与全部指定禁区版本无碰撞"

    clearance_payload: dict | None = None
    if payload.include_min_clearance and min_clr is not None:
        clearance_payload = {
            "min_clearance_mm": min_clr.net,
            "line_no": min_clr.line_no,
            "point_mm": list(min_clr.point),
            "zone_no": min_clr.zone_no,
            "progress": min_clr.progress,
        }
        if multi_tool:
            clearance_payload["tool_no"] = min_clr.tool_no
            clearance_payload["tool_radius_mm"] = min_clr.tool_radius

    report = Report(
        zone_versions=used_versions,
        input_summary=summary,
        has_collision=has_coll,
        collision_line_no=line_no,
        collision_zone_no=zone_no,
        conclusion=conclusion,
        collision_tool_no=collision_tool_no,
        collision_tool_radius=collision_tool_radius,
        min_clearance_enabled=bool(payload.include_min_clearance),
        min_clearance=clearance_payload,
        execution_trace=trace_payload,
    )
    db.add(report)
    db.commit()
    db.refresh(report)

    return ReviewResponse(
        report_id=report.id, used_zone_versions=used_versions,
        input_summary=summary, has_collision=has_coll,
        collision_line_no=line_no, collision_zone_no=zone_no,
        conclusion=conclusion, message=message,
        min_clearance=MinClearanceOut(**clearance_payload)
        if clearance_payload is not None else None,
        min_clearance_enabled=bool(payload.include_min_clearance),
        collision_tool_no=collision_tool_no,
        collision_tool_radius_mm=collision_tool_radius,
        execution_trace=[TraceEventOut(**e) for e in trace_payload]
        if trace_payload is not None else None)


@app.get("/reports/{report_id}", summary="查询不可变审查报告")
def get_report(report_id: int, db: Session = Depends(get_db)) -> dict:
    report = db.get(Report, report_id)
    if report is None:
        raise HTTPException(status_code=404, detail="报告不存在")
    return {
        "report_id": report.id,
        "created_at": report.created_at,
        "used_zone_versions": report.zone_versions,
        "input_summary": report.input_summary,
        "has_collision": report.has_collision,
        "collision_line_no": report.collision_line_no,
        "collision_zone_no": report.collision_zone_no,
        "conclusion": report.conclusion,
        "message": (
            f"发现碰撞：G-code 第 {report.collision_line_no} 行与禁区编号 "
            f"{report.collision_zone_no} 冲突"
            if report.has_collision and report.collision_line_no is not None
            else ("初始刀位点位于禁区范围内" if report.has_collision
                  else "刀路与全部指定禁区版本无碰撞")),
        # 多刀模式固化：命中刀号（初始刀位为 null）与毫米半径；
        # 旧报告/单刀旧请求这两列为缺省 null
        "collision_tool_no": report.collision_tool_no,
        "collision_tool_radius_mm": report.collision_tool_radius,
        # 固化的可选最小净间隙：未启用的旧报告不含/为空，查询保持一致
        "min_clearance": report.min_clearance,
        "min_clearance_enabled": bool(report.min_clearance_enabled),
        # 多刀模式固化的执行事件轨迹；单刀请求与旧报告为 null
        "execution_trace": report.execution_trace,
    }


@app.get("/health", summary="健康检查")
def health() -> dict:
    return {"status": "ok"}


def __sha(text: str) -> str:
    import hashlib
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
