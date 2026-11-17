"""URL过滤接口：维护过滤规则与规则版本快照。

规则版本接口负责发布/废弃不可变快照；发布时的模式合法性校验在 service 层
完成，非法模式返回 ok=false 的明确失败结果，不产生带病版本。

注意：静态路径（/export、/rule-versions）必须声明在参数路径 /{entry_id}
之前，否则会被 entry_id 路由吞掉并报 422。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query

from app.schemas import ActionResult, EntryPayload, PageResult
from app.services.urlfilter import SNAPSHOT_FIELDS, UrlfilterService

router = APIRouter(prefix="/api/urlfilter", tags=["URL过滤"])

service = UrlfilterService()

LIST_FIELDS = ["规则编号", "包含模式", "排除模式", "域名白名单", "路径黑名单", "参数剥离规则", "大小写处理", "尾部斜杠策略"]
STATUSES = ["草稿", "规则评审中", "已生效", "已废弃"]


# ------------------------------------------------------------- 规则版本快照（先）
@router.get("/rule-versions", response_model=PageResult[dict])
def list_rule_versions(
    keyword: str | None = Query(default=None, description="按版本号或规则编号检索"),
    status: str | None = Query(default=None, description="已生效、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """列出规则版本快照，新作业按最新生效版本取口径。"""
    items, total = service.list_versions(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/rule-versions", response_model=ActionResult)
def publish_rule_version(payload: EntryPayload) -> ActionResult:
    """发布不可变规则版本快照。

    包含规则、排除条件、参数范围、判定标准全部可配置；模式在发布时编译，
    非法模式返回明确失败，快照不会落库、不会生效（fail-closed）。
    """
    values = payload.values
    unknown = set(values) - set(SNAPSHOT_FIELDS)
    if unknown:
        return ActionResult(ok=False, message=f"存在不可配置字段：{'、'.join(sorted(unknown))}")
    entry, message = service.publish_version(values)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


@router.get("/rule-versions/export/all")
def export_rule_versions() -> dict[str, Any]:
    """导出全部规则版本快照（审计用）。"""
    return service.export_versions()


@router.get("/rule-versions/{version_id}", response_model=dict)
def get_rule_version(version_id: int) -> dict:
    """读取单份规则版本快照，供历史批次复核命中依据。"""
    entry = service.get_version(version_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"规则版本 {version_id} 不存在")
    return entry


@router.post("/rule-versions/{version_id}/deprecate", response_model=ActionResult)
def deprecate_rule_version(version_id: int) -> ActionResult:
    """废弃规则版本；快照保留，历史批次仍可按旧版本复核。"""
    entry, message = service.deprecate_version(version_id)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)


# ------------------------------------------------------------- 过滤规则（后）
@router.get("", response_model=PageResult[dict])
def list_entries(
    keyword: str | None = Query(default=None, description="按规则编号检索"),
    status: str | None = Query(default=None, description="草稿、规则评审中、已生效、已废弃"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """按规则编号与状态过滤URL过滤列表；没有数据时返回空页，不报错。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_entries(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/export")
def export_entries() -> dict[str, Any]:
    """导出URL过滤清单：返回当前过滤条件下的全量数据。"""
    items, total = service.list_entries(page=1, size=10000)
    return {"module": "urlfilter", "total": total, "items": items}


@router.post("", response_model=ActionResult)
def create_entry(payload: EntryPayload) -> ActionResult:
    """登记一条过滤规则，缺字段时说明原因而不是静默丢弃。"""
    entry, missing = service.create_entry(payload.values)
    if missing:
        return ActionResult(ok=False, message=f"缺少必填字段：{'、'.join(missing)}")
    return ActionResult(ok=True, message="过滤规则已登记", entry=entry)


@router.get("/{entry_id}", response_model=dict)
def get_entry(entry_id: int) -> dict:
    """读取单条过滤规则明细；不存在时给出可读的错误说明。"""
    entry = service.get_entry(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"过滤规则 {entry_id} 不存在或已归档")
    return entry


@router.post("/{entry_id}/actions", response_model=ActionResult)
def run_action(entry_id: int, payload: EntryPayload) -> ActionResult:
    """对单条过滤规则执行提交规则评审、生效规则、废弃规则；不允许的动作会被拦下并说明原因。"""
    action = str(payload.values.get("action") or "").strip()
    entry, message = service.run_action(entry_id, action)
    if entry is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=entry)
