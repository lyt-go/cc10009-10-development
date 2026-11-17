"""URL 规范化与过滤规则评估引擎。

只依赖标准库，承担两类纯计算职责：

1. ``normalize_url``：按可配置口径（大小写、尾斜杠、参数顺序、参数剥离、
   片段处理）把候选 URL 规范化，并产出用于去重的 canonical key。
2. ``RuleConfig.evaluate``：用一份不可变的规则版本快照评估规范化结果，
   记录完整命中依据。规则非法或校验失败时显式失败（fail-closed），
   绝不默认放行。
"""
from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit, urlunsplit

# 评估结论常量，避免在各模块里散落字符串字面量。
DECISION_ADMIT = "准入"
DECISION_REJECT = "拒绝"
DECISION_INVALID = "校验失败"

# 失败原因码：消费侧与审计明细都靠这些稳定码做口径统一。
REASON_INVALID_URL = "非法URL"
REASON_SCHEME = "协议不在允许范围"
REASON_WHITELIST_MISS = "未命中域名白名单"
REASON_PATH_BLACKLIST = "命中路径黑名单"
REASON_INCLUDE_MISS = "未命中包含模式"
REASON_EXCLUDE_HIT = "命中排除模式"
REASON_BAD_PATTERN = "非法过滤模式"
REASON_RULE_INACTIVE = "规则版本未生效"

DEFAULT_ALLOWED_SCHEMES = ("http", "https")
MAX_URL_LENGTH = 2048


class RuleValidationError(ValueError):
    """规则模式本身非法（例如正则无法编译），发布与评估都必须显式失败。"""

    def __init__(self, message: str, *, field_name: str | None = None, pattern: str | None = None) -> None:
        super().__init__(message)
        self.field_name = field_name
        self.pattern = pattern


class UrlValidationError(ValueError):
    """候选 URL 无法通过规范化校验，调用方据此生成明确的失败记录。"""


@dataclass(frozen=True)
class NormalizedUrl:
    """规范化结果：同时保留展示用 URL 与去重用 canonical key。"""

    raw_url: str
    normalized_url: str
    canonical_key: str
    scheme: str
    host: str
    port: int | None
    path: str
    kept_params: tuple[tuple[str, str], ...]
    stripped_params: tuple[str, ...]
    fragment_removed: bool


@dataclass(frozen=True)
class EvaluationResult:
    """单条 URL 的评估结论与命中依据，整条都可入审计明细。"""

    decision: str
    reason_code: str
    reason: str
    hits: tuple[dict[str, Any], ...] = field(default_factory=tuple)

    @property
    def admitted(self) -> bool:
        return self.decision == DECISION_ADMIT


def _compile_pattern(pattern: str, *, field_name: str) -> tuple[str, "re.Pattern[str] | None"]:
    """编译单个模式。

    - ``glob:`` 前缀按通配符（``*``/``?``）处理，转成等价正则；
    - 其余按 Python 正则处理，编译失败抛 :class:`RuleValidationError`；
    - 空串视为“不约束”，返回 ``(pattern, None)``。
    """
    text = (pattern or "").strip()
    if not text:
        return text, None
    try:
        if text.startswith("glob:"):
            regex = fnmatch.translate(text[len("glob:"):].strip())
            return text, re.compile(regex)
        return text, re.compile(text)
    except re.error as exc:
        raise RuleValidationError(
            f"字段「{field_name}」存在非法模式 {text!r}：{exc}",
            field_name=field_name,
            pattern=text,
        ) from exc


def _compile_patterns(patterns: Any, *, field_name: str) -> tuple[tuple[str, re.Pattern[str]], ...]:
    """批量编译模式列表/逗号分隔字符串；任一非法即整体失败，不静默跳过。"""
    if patterns is None:
        return ()
    if isinstance(patterns, str):
        items = [part.strip() for part in patterns.split(",") if part.strip()]
    else:
        items = [str(part).strip() for part in patterns if str(part).strip()]
    compiled: list[tuple[str, re.Pattern[str]]] = []
    for item in items:
        text, regex = _compile_pattern(item, field_name=field_name)
        if regex is not None:
            compiled.append((text, regex))
    return tuple(compiled)


@dataclass(frozen=True)
class RuleConfig:
    """一份不可变的规则版本快照。

    快照在发布时冻结，之后规则换版不会影响旧作业保留的历史依据，
    满足“旧作业仍保留原规则依据”的复核要求。
    """

    version: str
    规则编号: str
    域名白名单: tuple[str, ...]
    路径黑名单: tuple[tuple[str, re.Pattern[str]], ...]
    包含模式: tuple[tuple[str, re.Pattern[str]], ...]
    排除模式: tuple[tuple[str, re.Pattern[str]], ...]
    参数剥离: tuple[str, ...]
    大小写处理: str  # lower / preserve
    尾部斜杠策略: str  # strip / keep
    去除片段: bool
    允许协议: tuple[str, ...]
    状态: str

    @classmethod
    def from_snapshot(cls, snapshot: dict[str, Any]) -> "RuleConfig":
        """从持久化的版本快照字典构造；模式在此处编译，非法即失败。"""
        return cls(
            version=str(snapshot.get("version", "")),
            规则编号=str(snapshot.get("规则编号", "")),
            域名白名单=tuple(snapshot.get("域名白名单", ())),
            路径黑名单=_compile_patterns(snapshot.get("路径黑名单", ()), field_name="路径黑名单"),
            包含模式=_compile_patterns(snapshot.get("包含模式", ()), field_name="包含模式"),
            排除模式=_compile_patterns(snapshot.get("排除模式", ()), field_name="排除模式"),
            参数剥离=tuple(snapshot.get("参数剥离", ())),
            大小写处理=str(snapshot.get("大小写处理", "lower")),
            尾部斜杠策略=str(snapshot.get("尾部斜杠策略", "strip")),
            去除片段=bool(snapshot.get("去除片段", True)),
            允许协议=tuple(snapshot.get("允许协议", DEFAULT_ALLOWED_SCHEMES)),
            状态=str(snapshot.get("状态", "已生效")),
        )

    def to_snapshot(self) -> dict[str, Any]:
        """序列化成可持久化的快照（正则还原为模式文本）。"""
        return {
            "version": self.version,
            "规则编号": self.规则编号,
            "域名白名单": list(self.域名白名单),
            "路径黑名单": [text for text, _ in self.路径黑名单],
            "包含模式": [text for text, _ in self.包含模式],
            "排除模式": [text for text, _ in self.排除模式],
            "参数剥离": list(self.参数剥离),
            "大小写处理": self.大小写处理,
            "尾部斜杠策略": self.尾部斜杠策略,
            "去除片段": self.去除片段,
            "允许协议": list(self.允许协议),
            "状态": self.状态,
        }

    def _hit(self, stage: str, target: str, matched: bool, pattern: str | None = None) -> dict[str, Any]:
        hit: dict[str, Any] = {
            "stage": stage,
            "target": target,
            "matched": matched,
            "规则版本": self.version,
            "规则编号": self.规则编号,
        }
        if pattern is not None:
            hit["pattern"] = pattern
        return hit

    def evaluate(self, normalized: NormalizedUrl) -> EvaluationResult:
        """按 协议→白名单→路径黑名单→包含→排除 的固定顺序评估。

        任一环节不通过即返回拒绝/失败结论并附命中依据；全部通过才准入。
        模式在快照发布时已编译，理论上不会在评估期再出现编译错误，
        但仍保留 fail-closed 兜底，防止外部写入脏快照。
        """
        try:
            if self.状态 != "已生效":
                return EvaluationResult(
                    DECISION_INVALID,
                    REASON_RULE_INACTIVE,
                    f"规则版本 {self.version} 状态为{self.状态}，不能用于准入评估",
                )

            # 1) 协议范围
            scheme_hit = self._hit("协议校验", normalized.scheme, normalized.scheme in self.允许协议)
            if normalized.scheme not in self.允许协议:
                return EvaluationResult(
                    DECISION_REJECT, REASON_SCHEME, REASON_SCHEME, (scheme_hit,)
                )

            # 2) 域名白名单：支持精确域与通配符（*.example.com）
            whitelist_matched = _host_in_whitelist(normalized.host, self.域名白名单)
            whitelist_hit = self._hit("域名白名单", normalized.host, whitelist_matched)
            if not whitelist_matched:
                return EvaluationResult(
                    DECISION_REJECT, REASON_WHITELIST_MISS, REASON_WHITELIST_MISS, (whitelist_hit,)
                )

            # 3) 路径黑名单（排除类，命中即拒绝）
            for text, regex in self.路径黑名单:
                try:
                    matched = regex.search(normalized.path) is not None
                except re.error as exc:  # 兜底：脏快照不放行
                    raise RuleValidationError(str(exc), field_name="路径黑名单", pattern=text) from exc
                hit = self._hit("路径黑名单", normalized.path, matched, text)
                if matched:
                    return EvaluationResult(
                        DECISION_REJECT, REASON_PATH_BLACKLIST, REASON_PATH_BLACKLIST, (hit,)
                    )

            # 4) 包含模式：配置了就必须至少命中一个；未配置视为不约束
            include_hits: list[dict[str, Any]] = []
            if self.包含模式:
                any_include = False
                target = normalized.normalized_url
                for text, regex in self.包含模式:
                    try:
                        matched = regex.search(target) is not None
                    except re.error as exc:
                        raise RuleValidationError(str(exc), field_name="包含模式", pattern=text) from exc
                    include_hits.append(self._hit("包含模式", target, matched, text))
                    any_include = any_include or matched
                if not any_include:
                    return EvaluationResult(
                        DECISION_REJECT,
                        REASON_INCLUDE_MISS,
                        REASON_INCLUDE_MISS,
                        tuple(include_hits),
                    )

            # 5) 排除模式：命中任意一个即拒绝
            for text, regex in self.排除模式:
                try:
                    matched = regex.search(normalized.normalized_url) is not None
                except re.error as exc:
                    raise RuleValidationError(str(exc), field_name="排除模式", pattern=text) from exc
                hit = self._hit("排除模式", normalized.normalized_url, matched, text)
                if matched:
                    return EvaluationResult(
                        DECISION_REJECT, REASON_EXCLUDE_HIT, REASON_EXCLUDE_HIT, (hit,)
                    )

            all_hits = [scheme_hit, whitelist_hit, *include_hits]
            return EvaluationResult(
                DECISION_ADMIT,
                "全部规则通过",
                f"通过协议、白名单、包含/排除共 {len(all_hits)} 项校验",
                tuple(all_hits),
            )
        except RuleValidationError as exc:
            # fail-closed：模式非法是明确的失败结果，绝不能默认放行
            return EvaluationResult(
                DECISION_INVALID,
                REASON_BAD_PATTERN,
                str(exc),
                (self._hit("规则合法性", exc.field_name or "规则模式", False, exc.pattern),),
            )


def _host_in_whitelist(host: str, whitelist: tuple[str, ...]) -> bool:
    if not whitelist:
        # 未配置白名单视为不约束（白名单本身是可选准入条件，不属于“校验失败”）
        return True
    host_lower = host.lower()
    for entry in whitelist:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry.startswith("*."):
            suffix = entry[1:]  # ".example.com"
            if host_lower.endswith(suffix) and host_lower != suffix.lstrip("."):
                return True
            # 同时允许裸域 example.com
            if host_lower == entry[2:]:
                return True
        elif host_lower == entry:
            return True
    return False


def normalize_url(
    raw_url: str,
    *,
    strip_params: tuple[str, ...] | list[str] = (),
    case_handling: str = "lower",
    trailing_slash: str = "strip",
    drop_fragment: bool = True,
    allowed_schemes: tuple[str, ...] | list[str] = DEFAULT_ALLOWED_SCHEMES,
) -> NormalizedUrl:
    """把候选 URL 规范化。

    规范化口径（均可配置）：

    - 协议与主机小写（``case_handling=lower`` 时路径也按小写比较）；
    - 默认端口折叠（http:80、https:443）；
    - 查询参数按名排序，剥离 ``strip_params`` 指定的跟踪类参数；
    - 尾斜杠按 ``trailing_slash`` 折叠（strip/keep）；
    - 片段默认去除。

    无法解析、缺主机、协议不允许或超长时抛 :class:`UrlValidationError`，
    由上层记录为明确的“校验失败”，不会进入清单。
    """
    if not isinstance(raw_url, str) or not raw_url.strip():
        raise UrlValidationError("URL 为空或不是字符串")
    candidate = raw_url.strip()
    if len(candidate) > MAX_URL_LENGTH:
        raise UrlValidationError(f"URL 长度超过 {MAX_URL_LENGTH} 字符上限")

    try:
        parts = urlsplit(candidate)
        # .port 对越界端口会抛 ValueError，统一纳入显式校验失败
        port = parts.port
        hostname = parts.hostname
    except ValueError as exc:
        raise UrlValidationError(f"URL 无法解析：{exc}") from exc

    scheme = parts.scheme.lower()
    if scheme not in {s.lower() for s in allowed_schemes}:
        raise UrlValidationError(f"协议 {parts.scheme or '（空）'} 不在允许范围 {list(allowed_schemes)}")
    host = hostname
    if not host:
        raise UrlValidationError("URL 缺少主机名，无法定位抓取目标")
    host = host.lower().rstrip(".")

    if (scheme == "http" and port == 80) or (scheme == "https" and port == 443):
        port = None
    netloc = host if port is None else f"{host}:{port}"

    # 路径：空路径归一为根路径；尾斜杠策略；大小写策略
    path = parts.path or "/"
    if trailing_slash == "strip" and len(path) > 1 and path.endswith("/"):
        path = path.rstrip("/")
        if not path:
            path = "/"
    if case_handling == "lower":
        path = path.lower()

    # 查询参数：剥离指定参数后按名排序，消除参数顺序造成的重复页面
    strip_set = {name.lower() for name in strip_params}
    kept: list[tuple[str, str]] = []
    stripped: list[str] = []
    for key, value in _split_query_preserve_blank(parts.query):
        if key.lower() in strip_set:
            stripped.append(key)
            continue
        kept.append((key, value))
    kept.sort(key=lambda item: (item[0], item[1]))
    query = "&".join(f"{key}={value}" for key, value in kept)

    fragment_removed = bool(parts.fragment) and drop_fragment
    fragment = "" if drop_fragment else parts.fragment

    normalized_url = urlunsplit((scheme, netloc, path, query, fragment))

    # canonical key 是去重的唯一口径：强制小写路径、折叠尾斜杠、无片段
    canonical_path = path if case_handling == "lower" else path.lower()
    if len(canonical_path) > 1 and canonical_path.endswith("/"):
        canonical_path = canonical_path.rstrip("/") or "/"
    canonical_key = urlunsplit(
        (scheme, netloc, canonical_path, "&".join(f"{k.lower()}={v}" for k, v in kept), "")
    )

    return NormalizedUrl(
        raw_url=raw_url,
        normalized_url=normalized_url,
        canonical_key=canonical_key,
        scheme=scheme,
        host=host,
        port=port,
        path=path,
        kept_params=tuple(kept),
        stripped_params=tuple(stripped),
        fragment_removed=fragment_removed,
    )


def _split_query_preserve_blank(query: str) -> list[tuple[str, str]]:
    """拆分查询串，保留空值参数，容忍没有 ``=`` 的裸参数。"""
    if not query:
        return []
    pairs: list[tuple[str, str]] = []
    for chunk in query.split("&"):
        if not chunk:
            continue
        if "=" in chunk:
            key, value = chunk.split("=", 1)
        else:
            key, value = chunk, ""
        pairs.append((key, value))
    return pairs
