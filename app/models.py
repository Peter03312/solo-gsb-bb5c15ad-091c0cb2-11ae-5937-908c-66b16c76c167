"""ORM 模型。

- zones：禁区逻辑实体（编号唯一）；
- zone_versions：禁区的不可变版本，每次修改产生新版本，旧版本保留；
- reports：审查报告，创建后不可变，不随后续禁区版本变化；
- batches：批量审查的首次逐项结果快照（batch_id 唯一），用于幂等重放与冲突判定。
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import (JSON, CheckConstraint, Column, DateTime, Float,
                        ForeignKey, Index, Integer, String, Text, Boolean)
from sqlalchemy.orm import relationship

from .database import Base


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Zone(Base):
    __tablename__ = "zones"

    id = Column(Integer, primary_key=True)
    zone_no = Column(Integer, nullable=False, unique=True)
    description = Column(String(500), nullable=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    versions = relationship("ZoneVersion", back_populates="zone",
                            order_by="ZoneVersion.version")


class ZoneVersion(Base):
    __tablename__ = "zone_versions"
    __table_args__ = (
        CheckConstraint("version >= 1", name="ck_version_positive"),
        Index("ix_zone_version", "zone_id", "version", unique=True),
    )

    id = Column(Integer, primary_key=True)
    zone_id = Column(Integer, ForeignKey("zones.id"), nullable=False)
    version = Column(Integer, nullable=False)
    # 毫米制二维多边形顶点 [[x, y], ...]
    polygon = Column(JSON, nullable=False)
    z_min = Column(Float, nullable=False)
    z_max = Column(Float, nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    zone = relationship("Zone", back_populates="versions")


class Report(Base):
    """审查报告：保存即不可变。"""

    __tablename__ = "reports"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    # 审查所用禁区版本快照（zone_no -> version_id/version）
    zone_versions = Column(JSON, nullable=False)
    # 输入摘要
    input_summary = Column(JSON, nullable=False)

    has_collision = Column(Boolean, nullable=False)
    collision_line_no = Column(Integer, nullable=True)
    collision_zone_no = Column(Integer, nullable=True)
    conclusion = Column(Text, nullable=False)

    # 多刀模式/命中固化：首碰撞发生时生效刀号（初始刀位为空）与半径（mm）。
    # 旧请求/旧报告这两列为 NULL，读取保持兼容。
    collision_tool_no = Column(Integer, nullable=True)
    collision_tool_radius = Column(Float, nullable=True)

    # 可选的全程最小水平净间隙（仅 include_min_clearance=true 的报告固化）
    min_clearance_enabled = Column(Boolean, nullable=False, default=False,
                                   server_default="0")
    # {min_clearance_mm, line_no, point_mm, zone_no, progress,
    #  tool_no, tool_radius_mm} 或 None
    min_clearance = Column(JSON, nullable=True)

    # 多刀模式固化的执行事件轨迹（仅 tool_radii 启用的报告）：
    # [{seq, kind, line_no, tool_no, tool_radius_mm, start_mm, end_mm,
    #   hit_zone_no}, ...]；单刀请求与旧报告为 NULL
    execution_trace = Column(JSON, nullable=True)


class Batch(Base):
    """批量审查批次：同一 batch_id 的首次逐项结果快照（落库即不可变）。

    仅当整批 1~50 项任务全部校验通过时才在同一事务中与全部报告一起落库；
    校验失败的批次不落库、不占用 batch_id。相同 batch_id 重放时按
    content_hash 判定：内容一致 => 原样返回首次逐项结果；不一致 => 409 冲突。
    """

    __tablename__ = "batches"

    id = Column(Integer, primary_key=True)
    created_at = Column(DateTime(timezone=True), nullable=False, default=_utcnow)

    # 调用方提供的批次标识（全库唯一）
    batch_id = Column(String(128), nullable=False, unique=True)
    # 提交任务列表规范化（JSON 键序无关）后的 SHA-256，用于重放/冲突判定
    content_hash = Column(String(64), nullable=False)
    item_count = Column(Integer, nullable=False)
    # 首次逐项结果快照：[{index, report_id, ...单项审查结果}]，重放时原样返回
    items = Column(JSON, nullable=False)
