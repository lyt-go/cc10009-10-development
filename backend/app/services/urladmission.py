"""URL 准入业务规则：候选 URL → 规范化 → 规则评估 → 去重清单 → 抓取消费侧。

关键约束（对应需求口径）：

- 同一规范化 URL（canonical key）在同一批次内只能进入清单一次；
- 规则校验失败给出明确失败结果，绝不默认放行（fail-closed）；
- 批次处理幂等：同一份候选集 + 同一规则版本重复提交，只生效一次，
  并发回执也不会产生第二份结果（进程内锁 + 幂等键）；
- 每条准入记录保留规范化前后值、命中依据、规则版本，支持审计复核；
- 消费侧清单由准入结果驱动写入，写回抓取状态时同步更新本侧准入记录；
- 规则换版只影响新批次，历史批次永久保留发布时的规则快照依据。
"""
from __future__ import annotations

import hashlib
import threading
from datetime import datetime
from typing import Any

from app.store import store
from app.services.url_rules import (
    DECISION_ADMIT,
    DECISION_INVALID,
    DECISION_REJECT,
    REASON_INVALID_URL,
    RuleConfig,
    UrlValidationError,
    normalize_url,
)
from app.services.urlfilter import UrlfilterService

BATCH_MODULE = "url_admission_batch"
RECORD_MODULE = "url_admission_record"
CONSUMER_MODULE = "crawl_consumer_list"

# 抓取消费侧状态机：准入即待抓取，消费结果回写驱动后续流转。
CRAWL_STATUS_PENDING = "待抓取"
CRAWL_STATUS_RUNNING = "抓取中"
CRAWL_STATUS_SUCCESS = "抓取成功"
CRAWL_STATUS_FAILED = "抓取失败"
CRAWL_STATUS_SKIPPED = "已跳过"
WRITABLE_CRAWL_STATUSES = {
    CRAWL_STATUS_RUNNING,
    CRAWL_STATUS_SUCCESS,
    CRAWL_STATUS_FAILED,
    CRAWL_STATUS_SKIPPED,
}

# 进程内串行化：保证并发提交同一幂等键时只有一个线程真正执行管线。
_PIPELINE_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _next_id(module: str) -> int:
    rows = store.rows(module)
    return max((int(row.get("id", 0)) for row in rows), default=0) + 1


def _idempotency_key(candidate_urls: list[str], version_code: str) -> str:
    """幂等键 = 规则版本 + 排序去空白后的候选集。

    候选顺序不影响键值，保证“同一批 URL 换个顺序重提”仍然命中同一批次。
    """
    joined = "\n".join(sorted(url.strip() for url in candidate_urls if url.strip()))
    digest = hashlib.sha256(f"{version_code}\n{joined}".encode("utf-8")).hexdigest()
    return digest[:16]


class UrlAdmissionService:
    def __init__(self) -> None:
        self.rule_service = UrlfilterService()

    # ------------------------------------------------------------ 批次与记录查询
    def list_batches(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(BATCH_MODULE)
        if keyword:
            rows = [row for row in rows
                    if keyword in str(row.get("batch_no", "")) or keyword in str(row.get("规则版本", ""))]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        rows = sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_batch(self, batch_id: int) -> dict[str, Any] | None:
        return store.find(BATCH_MODULE, batch_id)

    def list_records(
        self,
        *,
        batch_id: int | None = None,
        decision: str | None = None,
        crawl_status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(RECORD_MODULE)
        if batch_id is not None:
            rows = [row for row in rows if row.get("批次ID") == batch_id]
        if decision:
            rows = [row for row in rows if row.get("准入结论") == decision]
        if crawl_status:
            rows = [row for row in rows if row.get("抓取状态") == crawl_status]
        if keyword:
            rows = [row for row in rows
                    if keyword in str(row.get("原始URL", "")) or keyword in str(row.get("规范化URL", ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_record(self, record_id: int) -> dict[str, Any] | None:
        return store.find(RECORD_MODULE, record_id)

    def find_admitted_record(self, canonical_key: str) -> dict[str, Any] | None:
        """按 canonical key 找已准入记录，供消费侧写回定位。"""
        for row in store.rows(RECORD_MODULE):
            if row.get("canonical_key") == canonical_key and row.get("准入结论") == DECISION_ADMIT:
                return row
        return None

    # ------------------------------------------------------------- 准入主管线
    def process_candidates(
        self,
        candidate_urls: list[str],
        *,
        version_code: str | None = None,
        idempotency_key: str | None = None,
        auto_sync_consumer: bool = True,
    ) -> tuple[dict[str, Any] | None, str]:
        """读取候选 URL 与生效规则，产出唯一一份准入结果与去重清单。

        返回 ``(批次, 消息)``；规则缺失、模式非法等场景返回 ``(None, 原因)``，
        由路由层给出失败回执，不会静默放行任何 URL。
        """
        urls = [str(url).strip() for url in candidate_urls or [] if str(url).strip()]
        if not urls:
            return None, "候选 URL 列表为空，无法生成准入结果"

        with _PIPELINE_LOCK:
            # 1) 定位生效规则版本快照（新作业默认取最新生效版本）
            version_row = (
                self.rule_service.find_version_by_code(version_code)
                if version_code
                else self.rule_service.latest_effective_version()
            )
            if version_row is None:
                wanted = version_code or "最新生效版本"
                return None, f"规则版本 {wanted} 不存在或未生效，按 fail-closed 拒绝处理本批 URL"
            try:
                config = RuleConfig.from_snapshot(version_row)
            except Exception as exc:  # 快照脏数据同样是明确失败，不放行
                return None, f"规则版本 {version_row.get('version')} 模式非法：{exc}"

            # 2) 幂等：同候选集 + 同版本只允许一份结果，并发重入直接返回既有批次
            idem_key = idempotency_key or _idempotency_key(urls, config.version)
            existing = self._find_batch_by_idempotency_key(idem_key)
            if existing is not None:
                return existing, f"批次 {existing['batch_no']} 已存在，重复处理只生效一次，直接返回既有结果"

            # 3) 逐条规范化 + 评估；canonical key 去重，同一规范 URL 只进清单一次
            records: list[dict[str, Any]] = []
            seen_keys: dict[str, int] = {}
            counters = {
                DECISION_ADMIT: 0,
                DECISION_REJECT: 0,
                DECISION_INVALID: 0,
                "duplicate": 0,
            }
            # 本批次内顺序分配记录 ID，保证“重复记录ID”指向的就是真实落库主键
            record_seq = max(
                (int(row.get("id", 0)) for row in store.rows(RECORD_MODULE)), default=0
            )

            def alloc_record_id() -> int:
                nonlocal record_seq
                record_seq += 1
                return record_seq

            for raw_url in urls:
                record = self._evaluate_one(raw_url, config, counters, seen_keys, alloc_record_id)
                records.append(record)

            batch_id = _next_id(BATCH_MODULE)
            batch_no = f"ADM-{batch_id:05d}"
            admitted_count = counters[DECISION_ADMIT]
            batch = {
                "id": batch_id,
                "batch_no": batch_no,
                "status": "已完成",
                "pending": False,
                "abnormal": counters[DECISION_INVALID] > 0,
                "幂等键": idem_key,
                "规则版本": config.version,
                "规则编号": config.规则编号,
                "规则快照": config.to_snapshot(),
                "候选总数": len(urls),
                "准入数": admitted_count,
                "拒绝数": counters[DECISION_REJECT],
                "校验失败数": counters[DECISION_INVALID],
                "重复剔除数": counters["duplicate"],
                "去重清单版本": f"{batch_no}@{config.version}",
                "创建时间": _now(),
                "消费侧同步": "未同步",
            }
            store.rows(BATCH_MODULE).append(batch)
            for record in records:
                record["批次ID"] = batch_id
                record["批次编号"] = batch_no
                store.rows(RECORD_MODULE).append(record)

            # 4) 处理结果驱动消费侧清单更新（幂等写入，重复同步不产生第二条）
            if auto_sync_consumer and admitted_count > 0:
                self._sync_batch_to_consumer(batch)
            elif admitted_count == 0:
                batch["消费侧同步"] = "无准入URL，无需同步"
            return batch, (
                f"批次 {batch_no} 处理完成：候选 {len(urls)} 条，准入 {admitted_count} 条，"
                f"拒绝 {counters[DECISION_REJECT]} 条，校验失败 {counters[DECISION_INVALID]} 条，"
                f"重复剔除 {counters['duplicate']} 条"
            )

    def _evaluate_one(
        self,
        raw_url: str,
        config: RuleConfig,
        counters: dict[str, int],
        seen_keys: dict[str, int],
        alloc_record_id,
    ) -> dict[str, Any]:
        """单条候选 URL 的规范化 + 评估，产出可审计的准入明细。"""
        record_id = alloc_record_id()
        base: dict[str, Any] = {
            "id": record_id,
            "原始URL": raw_url,
            "规则版本": config.version,
            "规则编号": config.规则编号,
            "抓取状态": CRAWL_STATUS_PENDING,
            "处理时间": _now(),
        }
        try:
            normalized = normalize_url(
                raw_url,
                strip_params=config.参数剥离,
                case_handling=config.大小写处理,
                trailing_slash=config.尾部斜杠策略,
                drop_fragment=config.去除片段,
                allowed_schemes=config.允许协议,
            )
        except UrlValidationError as exc:
            # 非法 URL：明确失败，绝不放行
            counters[DECISION_INVALID] += 1
            base.update({
                "准入结论": DECISION_INVALID,
                "原因码": REASON_INVALID_URL,
                "原因说明": str(exc),
                "规范化URL": None,
                "canonical_key": None,
                "命中依据": [],
                "规范化细节": None,
            })
            return base

        base["规范化URL"] = normalized.normalized_url
        base["canonical_key"] = normalized.canonical_key
        base["规范化细节"] = {
            "scheme": normalized.scheme,
            "host": normalized.host,
            "port": normalized.port,
            "path": normalized.path,
            "保留参数": list(normalized.kept_params),
            "剥离参数": list(normalized.stripped_params),
            "片段已去除": normalized.fragment_removed,
        }

        result = config.evaluate(normalized)
        base["准入结论"] = result.decision
        base["原因码"] = result.reason_code
        base["原因说明"] = result.reason
        base["命中依据"] = list(result.hits)

        if result.decision != DECISION_ADMIT:
            counters[result.decision] += 1
            return base

        # 仅对通过全部规则的 URL 做 canonical 去重
        if normalized.canonical_key in seen_keys:
            counters["duplicate"] += 1
            first_id = seen_keys[normalized.canonical_key]
            base["准入结论"] = DECISION_REJECT
            base["原因码"] = "规范化重复"
            base["原因说明"] = (
                f"与准入记录 #{first_id} 的 canonical key 相同，同一规范化URL只能进入清单一次"
            )
            base["命中依据"] = list(result.hits) + [{
                "stage": "规范化去重",
                "target": normalized.canonical_key,
                "matched": True,
                "重复记录ID": first_id,
                "规则版本": config.version,
            }]
            return base

        seen_keys[normalized.canonical_key] = record_id
        counters[DECISION_ADMIT] += 1
        return base

    def _find_batch_by_idempotency_key(self, idem_key: str) -> dict[str, Any] | None:
        for row in store.rows(BATCH_MODULE):
            if row.get("幂等键") == idem_key:
                return row
        return None

    # ------------------------------------------------------------- 消费侧清单同步
    def sync_batch_to_consumer(self, batch_id: int) -> tuple[dict[str, Any] | None, str]:
        """把去重后的 URL 清单写入抓取消费侧清单（幂等，可重复调用）。"""
        with _PIPELINE_LOCK:
            batch = store.find(BATCH_MODULE, batch_id)
            if batch is None:
                return None, f"准入批次 {batch_id} 不存在"
            return self._sync_batch_to_consumer(batch)

    def _sync_batch_to_consumer(self, batch: dict[str, Any]) -> tuple[dict[str, Any], str]:
        consumer_rows = store.rows(CONSUMER_MODULE)
        existing_keys = {row.get("canonical_key") for row in consumer_rows}
        admitted = [
            row for row in store.rows(RECORD_MODULE)
            if row.get("批次ID") == batch["id"] and row.get("准入结论") == DECISION_ADMIT
        ]
        created = 0
        skipped = 0
        for record in admitted:
            key = record["canonical_key"]
            if key in existing_keys:
                # 重复同步：不新增第二条，只登记来源批次，保证该侧记录仍是同一份
                self._append_source_batch(key, batch["batch_no"])
                skipped += 1
                continue
            consumer_rows.append({
                "id": _next_id(CONSUMER_MODULE),
                "canonical_key": key,
                "规范化URL": record["规范化URL"],
                "原始URL": record["原始URL"],
                "准入记录ID": record["id"],
                "来源批次": [batch["batch_no"]],
                "规则版本": record["规则版本"],
                "抓取状态": CRAWL_STATUS_PENDING,
                "入库时间": _now(),
                "最后回写时间": None,
                "抓取详情": None,
            })
            existing_keys.add(key)
            created += 1
        batch["消费侧同步"] = f"已同步（新增 {created} 条，幂等跳过 {skipped} 条）"
        batch["消费侧同步时间"] = _now()
        return batch, f"去重清单已写入抓取消费侧：新增 {created} 条，幂等跳过 {skipped} 条"

    def _append_source_batch(self, canonical_key: str, batch_no: str) -> None:
        for row in store.rows(CONSUMER_MODULE):
            if row.get("canonical_key") == canonical_key:
                sources = row.setdefault("来源批次", [])
                if batch_no not in sources:
                    sources.append(batch_no)
                return

    def list_consumer_entries(
        self,
        *,
        crawl_status: str | None = None,
        keyword: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(CONSUMER_MODULE)
        if crawl_status:
            rows = [row for row in rows if row.get("抓取状态") == crawl_status]
        if keyword:
            rows = [row for row in rows
                    if keyword in str(row.get("规范化URL", "")) or keyword in str(row.get("canonical_key", ""))]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    # ------------------------------------------------------------- 抓取状态回写
    def write_back_crawl_status(
        self,
        canonical_key: str,
        crawl_status: str,
        *,
        detail: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any] | None, str]:
        """抓取消费侧把实际抓取状态写回准入记录，两侧同步更新。"""
        if crawl_status not in WRITABLE_CRAWL_STATUSES:
            allowed = "、".join(sorted(WRITABLE_CRAWL_STATUSES))
            return None, f"抓取状态 {crawl_status!r} 不在可回写范围：{allowed}"
        with _PIPELINE_LOCK:
            consumer = None
            for row in store.rows(CONSUMER_MODULE):
                if row.get("canonical_key") == canonical_key:
                    consumer = row
                    break
            if consumer is None:
                return None, f"消费侧清单不存在 canonical_key={canonical_key} 的记录"
            record = store.find(RECORD_MODULE, int(consumer["准入记录ID"]))
            if record is None:
                return None, f"准入记录 {consumer['准入记录ID']} 已缺失，无法回写"

            timestamp = _now()
            consumer["抓取状态"] = crawl_status
            consumer["最后回写时间"] = timestamp
            consumer["抓取详情"] = detail or consumer.get("抓取详情")
            # 同步更新准入侧：抓取范围可从准入记录直接复核消费结果
            record["抓取状态"] = crawl_status
            record["抓取回写时间"] = timestamp
            record["抓取详情"] = detail or record.get("抓取详情")
            return record, f"抓取状态 {crawl_status} 已回写准入记录 #{record['id']}，两侧记录同步更新"

    def export_batch_records(self, batch_id: int) -> dict[str, Any]:
        """导出批次完整审计明细（含命中依据与规则快照）。"""
        batch = store.find(BATCH_MODULE, batch_id)
        records = [row for row in store.rows(RECORD_MODULE) if row.get("批次ID") == batch_id]
        return {"module": RECORD_MODULE, "batch": batch, "total": len(records), "items": records}
