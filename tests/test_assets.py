"""可复用资产库测试。

覆盖：版本发布与不可变、多层引用展开、环形引用检测、lock/follow
跟随策略、影响沿引用链传播、环境变量预览、批量升级（用例改引用 /
资产以发布新版本方式升级）。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import AssetError, AssetLibrary, EnvironmentManager, TestExecutor
from storage import StoreRegistry


class AssetLibraryTestBase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp()
        self.reg = StoreRegistry(os.path.join(self.root, "store"), shard_size=50)
        self.lib = AssetLibrary(self.reg)
        self.envm = EnvironmentManager(self.reg, self.root)
        self.reg.store("projects").insert({"id": "p1", "name": "demo"})
        self.cases = self.reg.store("cases")
        self.suites = self.reg.store("suites")

    def family(self, name, steps, category="step", tags=None):
        return self.lib.create_family("p1", {
            "name": name, "category": category, "tags": tags or [],
            "steps": steps})

    def case(self, cid, steps, name=None):
        record = {
            "id": cid, "project_id": "p1", "name": name or cid,
            "priority": "P2", "tags": [], "timeout": 60, "enabled": True,
            "steps": steps}
        self.cases.insert(record)
        return record

    @staticmethod
    def ref(aid, no, pin="follow"):
        return {"action": "use_asset", "asset_id": aid,
                "version": no, "pin": pin}


class TestVersioning(AssetLibraryTestBase):
    def test_versions_are_immutable_and_numbered(self):
        a = self.family("a", [{"action": "set", "key": "x", "value": 1}])
        v1 = self.lib.get_version(a["id"], 1)
        self.assertEqual(v1["steps"][0]["value"], 1)
        self.lib.publish_version(a["id"], {
            "steps": [{"action": "set", "key": "x", "value": 2}]})
        # 旧版本内容不变
        self.assertEqual(self.lib.get_version(a["id"], 1)["steps"][0]["value"], 1)
        self.assertEqual(self.lib.get_version(a["id"], 2)["steps"][0]["value"], 2)
        fam = self.lib.get_family(a["id"])
        self.assertEqual(fam["latest_version"], 2)
        self.assertEqual([v["version"] for v in self.lib.list_versions(a["id"])], [2, 1])

    def test_publish_duplicates_latest_when_no_steps(self):
        a = self.family("a", [{"action": "set", "key": "x", "value": 1}])
        self.lib.publish_version(a["id"], {})
        self.assertEqual(self.lib.get_version(a["id"], 2)["steps"][0]["value"], 1)

    def test_validation(self):
        with self.assertRaises(AssetError):
            self.family("bad", [], category="nope")
        with self.assertRaises(AssetError):
            self.lib.create_family("p1", {"name": "x", "steps": [
                {"action": "use_asset", "asset_id": "missing", "pin": "follow"}]})

    def test_lock_version_must_exist(self):
        a = self.family("a", [{"action": "set", "key": "x", "value": 1}])
        with self.assertRaises(AssetError):
            self.family("b", [self.ref(a["id"], 99, pin="lock")])

    def test_cannot_reference_self_directly(self):
        a = self.family("a", [{"action": "set", "key": "x", "value": 1}])
        with self.assertRaises(AssetError):
            self.lib.publish_version(a["id"], {"steps": [self.ref(a["id"], 1)]})


class TestExpand(AssetLibraryTestBase):
    def test_multi_level_expand_with_trace(self):
        leaf = self.family("leaf", [{"action": "set", "key": "l", "value": 1}])
        mid = self.family("mid", [self.ref(leaf["id"], 1),
                                  {"action": "set", "key": "m", "value": 2}])
        top = self.family("top", [self.ref(mid["id"], 1)])
        out = self.lib.expand_steps([self.ref(top["id"], 1)])
        actions = [(s["action"], s.get("key")) for s in out]
        self.assertEqual(actions, [("set", "l"), ("set", "m")])
        self.assertIn("leaf/v1", out[0]["_asset_path"])
        self.assertIn("top/v1", out[0]["_asset_path"])

    def test_cycle_detected_at_expand_time(self):
        a = self.family("a", [{"action": "set", "key": "x", "value": 1}])
        b = self.family("b", [self.ref(a["id"], 1)])
        # b 新版本引用 a，a 新版本引用 b v2 -> 环；a v1 仍可用
        self.lib.publish_version(b["id"], {"steps": [self.ref(a["id"], 1, "follow")]})
        self.lib.publish_version(a["id"], {"steps": [self.ref(b["id"], 2, "follow")]})
        with self.assertRaises(AssetError):
            self.lib.expand_steps([self.ref(a["id"], None, "follow")])
        # 锁定旧版本不受环影响
        out = self.lib.expand_steps([self.ref(a["id"], 1, "lock")])
        self.assertEqual(out[0]["key"], "x")

    def test_follow_vs_lock_resolution(self):
        a = self.family("a", [{"action": "set", "key": "u", "value": "v1"}])
        self.lib.publish_version(a["id"], {
            "steps": [{"action": "set", "key": "u", "value": "v2"}]})
        follow = self.lib.expand_steps([self.ref(a["id"], 1, "follow")])
        locked = self.lib.expand_steps([self.ref(a["id"], 1, "lock")])
        self.assertEqual(follow[0]["value"], "v2")
        self.assertEqual(locked[0]["value"], "v1")

    def test_missing_asset_raises(self):
        with self.assertRaises(AssetError):
            self.lib.expand_steps([{"action": "use_asset",
                                    "asset_id": "ast_gone", "pin": "follow"}])


class TestImpact(AssetLibraryTestBase):
    def _setup_chain(self):
        # base <- combo(资产引用资产) <- 用例 follow / lock
        base = self.family("base", [{"action": "set", "key": "b", "value": 1}])
        combo = self.family("combo", [self.ref(base["id"], 1, "follow")])
        cf = self.case("cf", [self.ref(combo["id"], 1, "follow")], "跟随用例")
        cl = self.case("cl", [self.ref(combo["id"], 1, "lock")], "锁定用例")
        self.suites.insert({"id": "s1", "project_id": "p1", "name": "套件",
                            "case_ids": ["cf", "cl"]})
        return base, combo, cf, cl

    def test_impact_propagation_and_pin_distinction(self):
        base, combo, cf, cl = self._setup_chain()
        # 发布 combo v2（combo 的引用方受影响）
        self.lib.publish_version(combo["id"], {
            "steps": [self.ref(base["id"], 1, "follow"),
                      {"action": "set", "key": "c", "value": 2}]})
        imp = self.lib.impact_of(combo["id"])
        by_id = {c["case_id"]: c for c in imp["cases"]}
        self.assertTrue(by_id["cf"]["outdated"])      # follow -> 立即变
        self.assertFalse(by_id["cl"]["outdated"])     # lock -> 结果不变
        self.assertTrue(by_id["cl"]["stale"])         # 但有新版可升
        self.assertEqual(imp["suites"][0]["case_count"], 2)

    def test_change_to_leaf_propagates_through_follow_chain(self):
        base, combo, cf, cl = self._setup_chain()
        # 改 base：combo follow base、cf follow combo -> cf 受影响
        self.lib.publish_version(base["id"], {
            "steps": [{"action": "set", "key": "b", "value": 2}]})
        imp = self.lib.impact_of(base["id"])
        by_id = {c["case_id"]: c for c in imp["cases"]}
        self.assertIn("cf", by_id)
        self.assertTrue(by_id["cf"]["outdated"])
        # cl 锁定 combo v1，而 combo v1 follow base —— 其展开结果同样会变
        self.assertIn("cl", by_id)
        self.assertTrue(by_id["cl"]["outdated"])
        asset_ids = {a["asset_id"] for a in imp["assets"]}
        self.assertIn(combo["id"], asset_ids)

    def test_locked_chain_does_not_propagate(self):
        base = self.family("base", [{"action": "set", "key": "b", "value": 1}])
        combo = self.family("combo", [self.ref(base["id"], 1, "lock")])
        self.case("cl", [self.ref(combo["id"], 1, "lock")], "双锁定")
        self.lib.publish_version(base["id"], {
            "steps": [{"action": "set", "key": "b", "value": 2}]})
        imp = self.lib.impact_of(base["id"])
        # combo 的最新版本锁定 base v1，不是脏版本；用例也不受影响
        self.assertEqual(imp["assets"], [])
        self.assertEqual(imp["outdated_case_count"], 0)


class TestPreviewAndEnvironment(AssetLibraryTestBase):
    def test_variable_sources_by_environment(self):
        a = self.family("envvars", [
            {"action": "set", "key": "BASE_URL", "value": "${BASE_URL}"},
            {"action": "request", "method": "GET", "url": "/health",
             "save_as": "resp"},
            {"action": "assert", "type": "status",
             "actual": "${resp.status}", "expected": 200},
        ], category="variables")
        case = self.case("c1", [self.ref(a["id"], 1)])
        dev = {"BASE_URL": "http://dev"}
        stg = {"BASE_URL": "http://stg"}
        p1 = self.lib.preview_case(case, dev)
        p2 = self.lib.preview_case(case, stg)
        v1 = {v["name"]: v for v in p1["variables"]}
        v2 = {v["name"]: v for v in p2["variables"]}
        self.assertEqual(v1["BASE_URL"]["value"], "http://dev")
        self.assertEqual(v2["BASE_URL"]["value"], "http://stg")
        self.assertEqual(v1["BASE_URL"]["source"], "environment")
        # save_as 产出的 resp 不应报未定义
        self.assertNotIn("resp", v1)

    def test_asset_default_used_for_internally_produced_var(self):
        # TOKEN 由资产用字面量产出：用例引用它不算「需要环境提供的输入」
        a = self.family("vars", [
            {"action": "set", "key": "TOKEN", "value": "default-token"}],
            category="variables")
        case = self.case("c1", [self.ref(a["id"], 1),
                                {"action": "assert", "type": "truthy",
                                 "actual": "${TOKEN}"}])
        p = self.lib.preview_case(case, {})
        self.assertNotIn("TOKEN", {v["name"] for v in p["variables"]})

    def test_executor_runs_expanded_case_with_env(self):
        a = self.family("health", [
            {"action": "request", "method": "GET", "url": "/api/health",
             "save_as": "resp"},
            {"action": "assert", "type": "status",
             "actual": "${resp.status}", "expected": 200},
        ])
        case = self.case("c1", [self.ref(a["id"], 1)])
        env = self.envm.create("p1", {"name": "dev", "variables": {},
                                      "config": {"latency_ms": 0}})
        ex = TestExecutor(step_expander=self.lib.expand_steps)
        result = ex.execute_case(case, self.envm.to_executor_config(env["id"]))
        self.assertEqual(result["status"], "passed")
        # 日志/步骤是展开后的，多于原始 1 步
        self.assertEqual(len(result["steps"]), 2)


class TestBatchUpgrade(AssetLibraryTestBase):
    def test_upgrade_locked_case_references(self):
        a = self.family("a", [{"action": "set", "key": "x", "value": 1}])
        self.lib.publish_version(a["id"], {
            "steps": [{"action": "set", "key": "x", "value": 2}]})
        self.case("locked", [self.ref(a["id"], 1, "lock")])
        self.case("follow", [self.ref(a["id"], 1, "follow")])
        r = self.lib.batch_upgrade(a["id"], scope="upgrade")
        self.assertEqual(r["changed_case_count"], 1)
        updated = self.cases.get("locked")
        self.assertEqual(updated["steps"][0]["version"], 2)
        self.assertEqual(updated["steps"][0]["pin"], "lock")
        # follow 用例不产生多余改动
        self.assertNotIn("follow", [c["case_id"] for c in r["changed_cases"]])

    def test_lock_scope_pins_followers(self):
        a = self.family("a", [{"action": "set", "key": "x", "value": 1}])
        self.lib.publish_version(a["id"], {
            "steps": [{"action": "set", "key": "x", "value": 2}]})
        self.case("cf", [self.ref(a["id"], 1, "follow")])
        self.lib.batch_upgrade(a["id"], scope="lock")
        self.assertEqual(self.cases.get("cf")["steps"][0]["pin"], "lock")
        self.assertEqual(self.cases.get("cf")["steps"][0]["version"], 2)

    def test_follow_scope_unlocks(self):
        a = self.family("a", [{"action": "set", "key": "x", "value": 1}])
        self.lib.publish_version(a["id"], {
            "steps": [{"action": "set", "key": "x", "value": 2}]})
        self.case("cl", [self.ref(a["id"], 1, "lock")])
        self.lib.batch_upgrade(a["id"], scope="follow")
        self.assertEqual(self.cases.get("cl")["steps"][0]["pin"], "follow")

    def test_asset_upgrade_publishes_new_version_keeps_old(self):
        # combo 锁定 base v1；base 发 v2 后批量升级：
        # 旧 combo v1 原样保留，升级通过发布 combo v2 完成
        base = self.family("base", [{"action": "set", "key": "x", "value": 1}])
        combo = self.family("combo", [self.ref(base["id"], 1, "lock")])
        self.lib.publish_version(base["id"], {
            "steps": [{"action": "set", "key": "x", "value": 2}]})
        r = self.lib.batch_upgrade(base["id"], scope="upgrade")
        self.assertEqual(r["changed_asset_count"], 1)
        self.assertEqual(self.lib.get_family(combo["id"])["latest_version"], 2)
        old = self.lib.get_version(combo["id"], 1)
        self.assertEqual(old["steps"][0]["version"], 1)
        new = self.lib.get_version(combo["id"], 2)
        self.assertEqual(new["steps"][0]["version"], 2)

    def test_search_and_tag_filter(self):
        self.family("登录流程", [{"action": "set", "key": "x", "value": 1}],
                    tags=["auth"])
        self.family("健康检查", [{"action": "set", "key": "y", "value": 1}],
                    tags=["smoke"])
        self.assertEqual(len(self.lib.list_families("p1", q="登录")), 1)
        self.assertEqual(len(self.lib.list_families("p1", tag="smoke")), 1)
        self.assertEqual(len(self.lib.list_families("p1", category="step")), 2)
        self.assertEqual(len(self.lib.list_families("p1", q="无关键词")), 0)


if __name__ == "__main__":
    unittest.main()
