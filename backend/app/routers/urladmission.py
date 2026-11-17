"""URL 准入接口：候选 URL 规范化、规则评估、去重清单与抓取消费侧回写。

所有业务判断都在 service 层；路由层只负责参数装配与回执翻译。
"""
from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

from app.schemas import ActionResult, PageResult
from app.services.urladmission import (
    DECISION_ADMIT,
    DECISION_INVALID,
    DECISION_REJECT,
    WRITABLE_CRAWL_STATUSES,
    UrlAdmissionService,
)

router = APIRouter(prefix="/api/urladmission", tags=["URL准入"])

service = UrlAdmissionService()


class CandidatePayload(BaseModel):
    """候选 URL 准入请求：候选集 + 可选规则版本 + 可选幂等键。"""

    candidate_urls: list[str] = Field(default_factory=list)
    规则版本: str | None = None
    idempotency_key: str | None = None
    auto_sync_consumer: bool = True


class WriteBackPayload(BaseModel):
    """抓取消费侧状态回写请求。"""

    canonical_key: str
    抓取状态: str
    抓取详情: dict[str, Any] | None = None


@router.post("/batches", response_model=ActionResult)
def process_candidates(payload: CandidatePayload) -> ActionResult:
    """读取候选 URL 与生效规则，生成唯一一份准入结果与去重清单。

    幂等：相同候选集与规则版本重复提交（含并发）只生效一次，
    第二次起返回既有批次，不会产生第二份结果。
    """
    batch, message = service.process_candidates(
        payload.candidate_urls,
        version_code=payload.规则版本,
        idempotency_key=payload.idempotency_key,
        auto_sync_consumer=payload.auto_sync_consumer,
    )
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/batches", response_model=PageResult[dict])
def list_batches(
    keyword: str | None = Query(default=None, description="按批次编号或规则版本检索"),
    status: str | None = Query(default=None, description="批次状态"),
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """分页列出准入批次，摘要里带候选/准入/拒绝/失败/重复计数。"""
    if size > 200:
        raise HTTPException(status_code=400, detail="每页最多 200 条，请缩小分页范围")
    items, total = service.list_batches(keyword=keyword, status=status, page=page, size=size)
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/batches/{batch_id}", response_model=dict)
def get_batch(batch_id: int) -> dict:
    """读取单个准入批次，含规则快照，可复核抓取范围口径。"""
    batch = service.get_batch(batch_id)
    if batch is None:
        raise HTTPException(status_code=404, detail=f"准入批次 {batch_id} 不存在")
    return batch


@router.get("/batches/{batch_id}/records", response_model=PageResult[dict])
def list_batch_records(
    batch_id: int,
    decision: str | None = Query(default=None, description=f"{DECISION_ADMIT}、{DECISION_REJECT}、{DECISION_INVALID}"),
    crawl_status: str | None = Query(default=None, description="按抓取状态筛选"),
    keyword: str | None = Query(default=None, description="按URL关键字检索"),
    page: int = 1,
    size: int = 50,
) -> PageResult[dict]:
    """读取批次下的准入明细：规范化结果、命中依据、抓取回写状态全保留。"""
    if service.get_batch(batch_id) is None:
        raise HTTPException(status_code=404, detail=f"准入批次 {batch_id} 不存在")
    if size > 500:
        raise HTTPException(status_code=400, detail="每页最多 500 条，请缩小分页范围")
    items, total = service.list_records(
        batch_id=batch_id,
        decision=decision,
        crawl_status=crawl_status,
        keyword=keyword,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/records", response_model=PageResult[dict])
def list_records(
    decision: str | None = None,
    crawl_status: str | None = None,
    keyword: str | None = None,
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """跨批次查询准入记录，便于按 URL 反查命中依据与规则版本。"""
    if size > 500:
        raise HTTPException(status_code=400, detail="每页最多 500 条，请缩小分页范围")
    items, total = service.list_records(
        decision=decision,
        crawl_status=crawl_status,
        keyword=keyword,
        page=page,
        size=size,
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.get("/batches/{batch_id}/export")
def export_batch(batch_id: int) -> dict[str, Any]:
    """导出批次完整审计明细（含规则快照与逐条命中依据）。"""
    if service.get_batch(batch_id) is None:
        raise HTTPException(status_code=404, detail=f"准入批次 {batch_id} 不存在")
    return service.export_batch_records(batch_id)


@router.post("/batches/{batch_id}/sync-consumer", response_model=ActionResult)
def sync_consumer(batch_id: int) -> ActionResult:
    """把去重后的 URL 清单写入抓取消费侧清单；重复调用幂等，不产生第二条。"""
    batch, message = service.sync_batch_to_consumer(batch_id)
    if batch is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=batch)


@router.get("/consumer-list", response_model=PageResult[dict])
def list_consumer(
    crawl_status: str | None = Query(default=None, description="待抓取、抓取中、抓取成功、抓取失败、已跳过"),
    keyword: str | None = None,
    page: int = 1,
    size: int = 20,
) -> PageResult[dict]:
    """读取抓取消费侧清单，每条记录都能回溯到准入记录与规则版本。"""
    if size > 500:
        raise HTTPException(status_code=400, detail="每页最多 500 条，请缩小分页范围")
    items, total = service.list_consumer_entries(
        crawl_status=crawl_status, keyword=keyword, page=page, size=size
    )
    return PageResult(items=items, total=total, page=page, size=size)


@router.post("/consumer-list/write-back", response_model=ActionResult)
def write_back(payload: WriteBackPayload) -> ActionResult:
    """抓取消费侧回写实际抓取状态，准入记录同步更新。"""
    if payload.抓取状态 not in WRITABLE_CRAWL_STATUSES:
        allowed = "、".join(sorted(WRITABLE_CRAWL_STATUSES))
        return ActionResult(ok=False, message=f"抓取状态不在可回写范围：{allowed}")
    record, message = service.write_back_crawl_status(
        payload.canonical_key, payload.抓取状态, detail=payload.抓取详情
    )
    if record is None:
        return ActionResult(ok=False, message=message)
    return ActionResult(ok=True, message=message, entry=record)
