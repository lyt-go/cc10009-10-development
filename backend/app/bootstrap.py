"""URL 准入演示数据引导。

仅依赖 service 层的正式入口写入，保证演示数据与真实管线产出结构完全一致：
先发布两份规则版本快照（含一版已废弃快照，体现换版），再跑一批候选 URL，
覆盖准入、拒绝、非法 URL、规范化重复、参数剥离、尾斜杠/大小写折叠等场景。
"""
from __future__ import annotations

from app.store import store
from app.services.urladmission import UrlAdmissionService
from app.services.urlfilter import VERSION_MODULE, UrlfilterService

BOOTSTRAP_FLAG = "urladmission_bootstrapped"

DEMO_RULE_V1 = {
    "规则编号": "URLF-RULE-V1",
    "域名白名单": ["example.com", "*.example.com"],
    "路径黑名单": [r"/private(/.*)?", r"/admin(/.*)?"],
    "包含模式": [r"^https?://"],
    "排除模式": [r"[?&]utm_block=1", r"\.(gif|jpg|png|css|js)(\?|$)"],
    "参数剥离": ["utm_source", "utm_medium", "utm_campaign", "sessionid"],
    "大小写处理": "lower",
    "尾部斜杠策略": "strip",
    "去除片段": True,
    "允许协议": ["http", "https"],
}

DEMO_RULE_V2 = {
    "规则编号": "URLF-RULE-V2",
    "域名白名单": ["example.com", "*.example.com", "docs.example.com"],
    "路径黑名单": [r"/private(/.*)?"],
    "包含模式": [r"^https?://"],
    "排除模式": [r"[?&]utm_block=1", r"/legacy(/.*)?"],
    "参数剥离": ["utm_source", "utm_medium", "utm_campaign", "sessionid", "spm"],
    "大小写处理": "lower",
    "尾部斜杠策略": "strip",
    "去除片段": True,
    "允许协议": ["http", "https"],
}

DEMO_CANDIDATES = [
    # 大小写 + 尾斜杠 + 参数顺序 + 跟踪参数差异，规范化后应只留一条
    "https://example.com/News/Article-1?utm_source=mail&id=42&page=1",
    "HTTPS://EXAMPLE.COM/news/article-1/?page=1&id=42",
    "https://example.com/news/article-1?sessionid=abc&page=1&id=42#section-2",
    # 正常准入的其它页面
    "https://docs.example.com/guide/quickstart",
    "http://blog.example.com/posts/2026/release-notes",
    # 命中路径黑名单 → 拒绝
    "https://example.com/private/secret",
    # 命中排除模式（静态资源）→ 拒绝
    "https://example.com/assets/banner.png",
    # 域名不在白名单 → 拒绝
    "https://evil.example.org/phish",
    # 协议不允许 → 拒绝
    "ftp://example.com/package.zip",
    # 非法 URL → 明确校验失败，不放行
    "http://[not-a-host",
    "not a url at all",
]


def bootstrap_demo_data() -> None:
    """幂等引导：已引导过（或版本表已有数据）则直接跳过。"""
    if getattr(store, BOOTSTRAP_FLAG, False):
        return
    if store.rows(VERSION_MODULE):
        setattr(store, BOOTSTRAP_FLAG, True)
        return

    rule_service = UrlfilterService()
    v1, message = rule_service.publish_version(DEMO_RULE_V1)
    if v1 is None:  # 演示规则本身必须合法，否则属于开发期错误
        raise RuntimeError(f"演示规则 V1 发布失败：{message}")
    rule_service.deprecate_version(int(v1["id"]))

    v2, message = rule_service.publish_version(DEMO_RULE_V2)
    if v2 is None:
        raise RuntimeError(f"演示规则 V2 发布失败：{message}")

    admission_service = UrlAdmissionService()
    batch, message = admission_service.process_candidates(
        DEMO_CANDIDATES, version_code=str(v2["version"])
    )
    if batch is None:
        raise RuntimeError(f"演示准入批次生成失败：{message}")

    # 给消费侧制造一条已回写的样例，展示两侧状态联动
    admitted_records, _ = admission_service.list_records(
        batch_id=int(batch["id"]), decision="准入", page=1, size=1
    )
    if admitted_records:
        first = admitted_records[0]
        admission_service.write_back_crawl_status(
            str(first["canonical_key"]),
            "抓取成功",
            detail={"http_status": 200, "bytes": 15360, "cost_ms": 82},
        )

    setattr(store, BOOTSTRAP_FLAG, True)
