"""URL 准入管线回归测试（仅标准库，python -I 可直接运行）。

覆盖需求口径：
1. 规范化折叠大小写/参数顺序/尾斜杠/片段/跟踪参数，且同一 canonical key 只进清单一次；
2. 白名单、路径黑名单、包含、排除、非法 URL 各自给出明确结论，绝不默认放行；
3. 非法模式发布即失败（fail-closed），无生效规则版本时整批拒绝处理；
4. 幂等：顺序重复、显式幂等键、并发重复都只产生一份批次；
5. 换版产生不同清单版本，旧批次保留旧快照依据；
6. 消费侧同步幂等，抓取状态回写后两侧记录同步更新。

运行：cd backend && .venv/bin/python -I tests/test_url_admission.py
"""
from __future__ import annotations

import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.url_rules import (  # noqa: E402
    DECISION_ADMIT,
    DECISION_INVALID,
    DECISION_REJECT,
    RuleConfig,
    RuleValidationError,
    UrlValidationError,
    normalize_url,
)
from app.services.urladmission import UrlAdmissionService  # noqa: E402
from app.services.urlfilter import UrlfilterService  # noqa: E402
from app.store import Store  # noqa: E402
import app.services.urladmission as admission_mod  # noqa: E402
import app.services.urlfilter as filter_mod  # noqa: E402


def make_rule(**overrides):
    base = {
        "version": "RV-TEST",
        "规则编号": "R-TEST",
        "域名白名单": ["example.com", "*.example.com"],
        "路径黑名单": [r"/private(/.*)?"],
        "包含模式": [r"^https?://"],
        "排除模式": [r"[?&]block=1"],
        "参数剥离": ["utm_source", "sessionid"],
        "大小写处理": "lower",
        "尾部斜杠策略": "strip",
        "去除片段": True,
        "允许协议": ["http", "https"],
        "状态": "已生效",
    }
    base.update(overrides)
    return RuleConfig.from_snapshot(base)


class FreshStoreMixin:
    """每个用例使用独立内存仓库，避免共享状态。"""

    def setUp(self):
        self.store = Store()
        # service 内部通过模块级单例 store 访问，这里替换它
        admission_mod.store = self.store
        filter_mod.store = self.store
        self.rule_service = UrlfilterService()
        self.service = UrlAdmissionService()
        version, message = self.rule_service.publish_version({
            "规则编号": "R1",
            "域名白名单": ["example.com", "*.example.com"],
            "路径黑名单": [r"/private(/.*)?"],
            "包含模式": [r"^https?://"],
            "排除模式": [r"[?&]block=1"],
            "参数剥离": ["utm_source", "sessionid"],
        })
        assert version is not None, message
        self.version_code = version["version"]


class NormalizeTests(unittest.TestCase):
    def test_case_slash_param_order_fragment_collapse(self):
        n1 = normalize_url("HTTPS://EXAMPLE.COM/News/A/?b=2&a=1#frag")
        n2 = normalize_url("https://example.com/news/a?a=1&b=2")
        self.assertEqual(n1.canonical_key, n2.canonical_key)
        self.assertEqual(n1.host, "example.com")
        self.assertEqual(n1.path, "/news/a")
        self.assertTrue(n1.fragment_removed)
        # 参数按名排序
        self.assertEqual(n1.normalized_url, "https://example.com/news/a?a=1&b=2")

    def test_param_stripping_and_default_port(self):
        n = normalize_url("http://example.com:80/x?utm_source=mail&keep=1", strip_params=("utm_source",))
        self.assertIsNone(n.port)
        self.assertEqual(n.stripped_params, ("utm_source",))
        self.assertIn("keep=1", n.normalized_url)
        self.assertNotIn("utm_source", n.normalized_url)

    def test_invalid_urls_raise(self):
        for bad in ("", "   ", "not a url", "http://[broken", "ftp://example.com/x",
                    "http://example.com:99999/x", "https:///missing-host"):
            with self.subTest(bad=bad):
                with self.assertRaises(UrlValidationError):
                    normalize_url(bad)

    def test_keep_mode_preserves_path_case_and_slash(self):
        n = normalize_url("https://example.com/Mixed/Path/",
                          case_handling="preserve", trailing_slash="keep")
        self.assertEqual(n.path, "/Mixed/Path/")


class RuleEvaluationTests(unittest.TestCase):
    def test_whitelist_blacklist_exclude_include(self):
        rule = make_rule()
        cases = {
            "https://example.com/ok": DECISION_ADMIT,
            "https://sub.example.com/ok": DECISION_ADMIT,
            "https://evil.org/x": DECISION_REJECT,  # 白名单不通过
            "https://example.com/private/secret": DECISION_REJECT,  # 路径黑名单
            "https://example.com/page?block=1": DECISION_REJECT,  # 排除模式
        }
        for url, expected in cases.items():
            with self.subTest(url=url):
                result = rule.evaluate(normalize_url(url))
                self.assertEqual(result.decision, expected)
                self.assertTrue(result.hits)  # 每条结论都有命中依据

    def test_bad_pattern_reports_field_and_pattern(self):
        # 非法模式在编译（发布）阶段即显式失败，错误带字段与模式信息，绝不带病生效
        with self.assertRaises(RuleValidationError) as ctx:
            make_rule(排除模式=["[unterminated"])
        self.assertEqual(ctx.exception.field_name, "排除模式")
        self.assertEqual(ctx.exception.pattern, "[unterminated")

    def test_inactive_rule_fails_closed(self):
        rule = make_rule(状态="草稿")
        result = rule.evaluate(normalize_url("https://example.com/x"))
        self.assertEqual(result.decision, DECISION_INVALID)

    def test_invalid_pattern_raises_at_compile(self):
        with self.assertRaises(RuleValidationError):
            make_rule(包含模式=["("])


class AdmissionPipelineTests(FreshStoreMixin, unittest.TestCase):
    def test_full_pipeline_dedup_and_audit_detail(self):
        urls = [
            "https://example.com/a?x=1&y=2",
            "HTTPS://EXAMPLE.COM/a/?y=2&x=1",          # 规范化重复
            "https://example.com/private/no",           # 黑名单拒绝
            "https://badhost.net/x",                    # 白名单拒绝
            "http://[broken",                           # 非法 URL
        ]
        batch, message = self.service.process_candidates(urls, version_code=self.version_code)
        self.assertIsNotNone(batch, message)
        self.assertEqual(batch["候选总数"], 5)
        self.assertEqual(batch["准入数"], 1)
        self.assertEqual(batch["重复剔除数"], 1)
        self.assertEqual(batch["拒绝数"], 2)
        self.assertEqual(batch["校验失败数"], 1)
        # 消费侧自动同步，且只含准入 URL
        consumer = self.store.rows("crawl_consumer_list")
        self.assertEqual(len(consumer), 1)
        # 审计明细齐全
        records = self.store.rows("url_admission_record")
        for record in records:
            self.assertIn("命中依据", record)
            self.assertIn("规则版本", record)
            self.assertIn("规范化细节", record)

    def test_duplicate_submission_idempotent(self):
        urls = ["https://example.com/idem?a=1", "https://example.com/other"]
        b1, _ = self.service.process_candidates(urls, version_code=self.version_code)
        b2, msg = self.service.process_candidates(list(reversed(urls)), version_code=self.version_code)
        self.assertEqual(b1["id"], b2["id"])
        self.assertIn("只生效一次", msg)
        self.assertEqual(len(self.store.rows("url_admission_batch")), 1)
        self.assertEqual(len(self.store.rows("url_admission_record")), 2)

    def test_explicit_idempotency_key(self):
        b1, _ = self.service.process_candidates(["https://example.com/k"], idempotency_key="K")
        b2, _ = self.service.process_candidates(["https://example.com/k-different"], idempotency_key="K")
        self.assertEqual(b1["id"], b2["id"])

    def test_concurrent_submission_single_result(self):
        urls = ["https://example.com/c?z=9", "https://example.com/c?z=9&utm_source=x"]
        ids = []

        def worker():
            batch, _ = self.service.process_candidates(urls, version_code=self.version_code)
            ids.append(batch["id"] if batch else None)

        threads = [threading.Thread(target=worker) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(len(ids), 10)
        self.assertEqual(set(ids), {ids[0]})
        self.assertEqual(len(self.store.rows("url_admission_batch")), 1)

    def test_no_effective_rule_fails_closed(self):
        for row in self.rule_service.list_versions(page=1, size=100)[0]:
            self.rule_service.deprecate_version(int(row["id"]))
        batch, message = self.service.process_candidates(["https://example.com/x"])
        self.assertIsNone(batch)
        self.assertIn("fail-closed", message)
        self.assertEqual(self.store.rows("url_admission_batch"), [])

    def test_invalid_pattern_publish_rejected(self):
        entry, message = self.rule_service.publish_version(
            {"规则编号": "BAD", "排除模式": ["[bad("]}
        )
        self.assertIsNone(entry)
        self.assertIn("非法模式", message)

    def test_rule_version_change_creates_new_list_version(self):
        urls = ["https://example.com/versioned"]
        b1, _ = self.service.process_candidates(urls, version_code=self.version_code)
        v2, _ = self.rule_service.publish_version({
            "规则编号": "R2", "域名白名单": ["example.com", "new.com"],
        })
        b2, _ = self.service.process_candidates(urls, version_code=v2["version"])
        self.assertNotEqual(b1["id"], b2["id"])
        self.assertNotEqual(b1["去重清单版本"], b2["去重清单版本"])
        # 旧批次仍保留旧规则快照
        self.assertEqual(b1["规则快照"]["version"], self.version_code)
        self.assertEqual(b1["规则版本"], self.version_code)

    def test_consumer_sync_idempotent_and_write_back(self):
        batch, _ = self.service.process_candidates(
            ["https://example.com/wb", "https://example.com/wb2"],
            version_code=self.version_code,
            auto_sync_consumer=False,
        )
        self.assertEqual(len(self.store.rows("crawl_consumer_list")), 0)
        _, msg1 = self.service.sync_batch_to_consumer(int(batch["id"]))
        _, msg2 = self.service.sync_batch_to_consumer(int(batch["id"]))
        self.assertEqual(len(self.store.rows("crawl_consumer_list")), 2)
        self.assertIn("新增 2", msg1)
        self.assertIn("跳过 2", msg2)

        key = self.store.rows("crawl_consumer_list")[0]["canonical_key"]
        record, msg = self.service.write_back_crawl_status(key, "抓取成功", detail={"code": 200})
        self.assertEqual(record["抓取状态"], "抓取成功")
        # 两侧同步
        consumer = [c for c in self.store.rows("crawl_consumer_list") if c["canonical_key"] == key][0]
        self.assertEqual(consumer["抓取状态"], "抓取成功")
        # 非法状态不允许回写
        bad, _ = self.service.write_back_crawl_status(key, "不存在的状态")
        self.assertIsNone(bad)

    def test_empty_candidates_rejected(self):
        batch, message = self.service.process_candidates([])
        self.assertIsNone(batch)
        self.assertIn("为空", message)


if __name__ == "__main__":
    unittest.main(verbosity=2)
