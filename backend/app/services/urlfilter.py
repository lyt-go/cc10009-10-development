"""URL过滤业务规则：状态流转、字段校验与筛选口径都收在这里。

在原有过滤规则登记/流转之上，新增**规则版本快照**能力：

- 发布版本时冻结白名单、排除模式、参数剥离等全部口径，并立即编译模式；
- 非法模式在发布环节就得到明确失败结果，不允许带病版本进入生效态；
- 快照一经发布不可修改，只能废弃；旧作业保留旧版本依据，换版不影响历史清单。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from app.store import store
from app.services.url_rules import (
    DEFAULT_ALLOWED_SCHEMES,
    RuleConfig,
    RuleValidationError,
)

MODULE = "urlfilter"
VERSION_MODULE = "urlfilter_rule_version"
REQUIRED_FIELDS = ["规则编号", "包含模式", "排除模式"]
STATUS_ORDER = ["草稿", "规则评审中", "已生效", "已废弃"]
ACTION_RULES = {"提交规则评审": "规则评审中", "生效规则": "已生效", "废弃规则": "已废弃"}
NEGATIVE_ACTIONS = []

VERSION_STATUS_ORDER = ["已生效", "已废弃"]
# 版本快照里可配置的口径字段；包含规则、排除条件、参数范围、判定标准都在此列。
SNAPSHOT_FIELDS = [
    "规则编号",
    "域名白名单",
    "路径黑名单",
    "包含模式",
    "排除模式",
    "参数剥离",
    "大小写处理",
    "尾部斜杠策略",
    "去除片段",
    "允许协议",
]
LIST_PATTERN_FIELDS = ("域名白名单", "路径黑名单", "包含模式", "排除模式", "参数剥离", "允许协议")
CASE_OPTIONS = ("lower", "preserve")
SLASH_OPTIONS = ("strip", "keep")


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def _as_list(value: Any) -> list[str]:
    """兼容逗号分隔字符串与数组两种提交形式。"""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(item).strip() for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


class UrlfilterService:
    # ------------------------------------------------------------------ 规则登记
    def list_entries(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("status") == status]
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_entry(self, entry_id: int) -> dict[str, Any] | None:
        return store.find(MODULE, entry_id)

    def create_entry(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, list[str]]:
        missing = [field for field in REQUIRED_FIELDS if not str(values.get(field) or "").strip()]
        if missing:
            return None, missing
        rows = store.rows(MODULE)
        entry = {"id": max((int(row.get("id", 0)) for row in rows), default=0) + 1}
        entry.update({field: values.get(field) for field in REQUIRED_FIELDS})
        entry["status"] = STATUS_ORDER[0]
        entry["pending"] = True
        entry["abnormal"] = False
        rows.append(entry)
        return entry, []

    def run_action(self, entry_id: int, action: str) -> tuple[dict[str, Any] | None, str]:
        entry = store.find(MODULE, entry_id)
        if entry is None:
            return None, f"过滤规则 {entry_id} 不存在或已归档"
        if action not in ACTION_RULES:
            return None, f"动作「{action}」不属于URL过滤可执行范围"
        target = ACTION_RULES[action]
        if target not in STATUS_ORDER:
            return None, f"目标状态「{target}」不在允许的状态序列里"
        entry["status"] = target
        entry["pending"] = target != STATUS_ORDER[-1]
        entry["abnormal"] = action in NEGATIVE_ACTIONS
        return entry, f"过滤规则已{action}"

    # -------------------------------------------------------------- 规则版本快照
    def list_versions(
        self,
        *,
        keyword: str | None = None,
        status: str | None = None,
        page: int = 1,
        size: int = 20,
    ) -> tuple[list[dict[str, Any]], int]:
        rows = store.rows(VERSION_MODULE)
        if keyword:
            rows = [row for row in rows if keyword in str(row.get("version", ""))
                    or keyword in str(row.get("规则编号", ""))]
        if status:
            rows = [row for row in rows if row.get("状态") == status]
        rows = sorted(rows, key=lambda row: int(row.get("id", 0)), reverse=True)
        total = len(rows)
        start = max(page - 1, 0) * size
        return rows[start:start + size], total

    def get_version(self, version_id: int) -> dict[str, Any] | None:
        return store.find(VERSION_MODULE, version_id)

    def find_version_by_code(self, version_code: str) -> dict[str, Any] | None:
        for row in store.rows(VERSION_MODULE):
            if row.get("version") == version_code and row.get("状态") == "已生效":
                return row
        return None

    def latest_effective_version(self) -> dict[str, Any] | None:
        """新作业默认使用最新生效版本；没有生效版本时返回 None（上层显式失败）。"""
        effective = [
            row for row in store.rows(VERSION_MODULE) if row.get("状态") == "已生效"
        ]
        if not effective:
            return None
        return sorted(effective, key=lambda row: int(row.get("id", 0)))[-1]

    def publish_version(self, values: dict[str, Any]) -> tuple[dict[str, Any] | None, str]:
        """发布一份不可变规则版本快照。

        发布前先按快照内容构造 :class:`RuleConfig` 完成模式编译，
        任一非法模式都会得到明确失败结果，且不会落库、不会生效。
        """
        rule_code = str(values.get("规则编号") or "").strip()
        if not rule_code:
            return None, "缺少必填字段：规则编号"

        case_handling = str(values.get("大小写处理") or "lower").strip()
        if case_handling not in CASE_OPTIONS:
            return None, f"大小写处理仅支持 {CASE_OPTIONS}，收到 {case_handling!r}"
        trailing_slash = str(values.get("尾部斜杠策略") or "strip").strip()
        if trailing_slash not in SLASH_OPTIONS:
            return None, f"尾部斜杠策略仅支持 {SLASH_OPTIONS}，收到 {trailing_slash!r}"

        snapshot = {
            "规则编号": rule_code,
            "域名白名单": _as_list(values.get("域名白名单")),
            "路径黑名单": _as_list(values.get("路径黑名单")),
            "包含模式": _as_list(values.get("包含模式")),
            "排除模式": _as_list(values.get("排除模式")),
            "参数剥离": _as_list(values.get("参数剥离")),
            "大小写处理": case_handling,
            "尾部斜杠策略": trailing_slash,
            "去除片段": bool(values.get("去除片段", True)),
            "允许协议": _as_list(values.get("允许协议")) or list(DEFAULT_ALLOWED_SCHEMES),
            "状态": "已生效",
        }

        # 关键闸门：编译不过就明确失败，绝不带非法模式放行
        try:
            config = RuleConfig.from_snapshot(snapshot)
        except RuleValidationError as exc:
            return None, f"规则版本发布失败：{exc}"

        rows = store.rows(VERSION_MODULE)
        next_id = max((int(row.get("id", 0)) for row in rows), default=0) + 1
        version_code = f"RV-{next_id:04d}"
        snapshot["version"] = version_code
        # 再做一次不可变对象构造，确认落库内容与编译口径完全一致
        RuleConfig.from_snapshot(snapshot)
        entry = {
            "id": next_id,
            "version": version_code,
            "status": "已生效",
            "pending": True,
            "abnormal": False,
            "published_at": _now(),
            **snapshot,
        }
        rows.append(entry)
        return entry, f"规则版本 {version_code} 已发布并生效"

    def deprecate_version(self, version_id: int) -> tuple[dict[str, Any] | None, str]:
        """废弃版本：快照本身保留，供历史作业复核，只是不再被新作业选用。"""
        entry = store.find(VERSION_MODULE, version_id)
        if entry is None:
            return None, f"规则版本 {version_id} 不存在"
        if entry.get("状态") == "已废弃":
            return entry, f"规则版本 {entry.get('version')} 已是废弃态"
        entry["状态"] = "已废弃"
        entry["status"] = "已废弃"
        entry["pending"] = False
        return entry, f"规则版本 {entry.get('version')} 已废弃，历史清单仍保留其判定依据"

    def export_versions(self) -> dict[str, Any]:
        rows = store.rows(VERSION_MODULE)
        return {"module": VERSION_MODULE, "total": len(rows), "items": rows}
