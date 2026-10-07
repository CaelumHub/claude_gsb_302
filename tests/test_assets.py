"""资产库单元测试。

覆盖：资产分类创建与发布、版本不可变、多层引用展开、形参作用域隔离、
环境变量跨环境取值、循环引用检测、影响分析（跟随 latest / 锁定旧版）、
过期引用与批量升级、删除保护、变量模板与预览替换。
"""

from __future__ import annotations

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from engine import (AssetError, AssetExpander, AssetLibrary, EnvironmentManager,
                    TestExecutor)
from engine.assets import (MAX_EXPAND_DEPTH, PIN_LATEST, _rewrite_vars,
                           substitute_env_vars)
from storage import StoreRegistry


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = StoreRegistry(os.path.join(self.tmp.name, "store"))
        self.lib = AssetLibrary(self.registry)
        self.ex = AssetExpander(self.registry)
        self.envm = EnvironmentManager(self.registry, self.tmp.name)
        self.pid = self.registry.store("projects").insert({"name": "P"})

    def tearDown(self):
        self.tmp.cleanup()

    def create(self, key, category, *, params=None, steps=None, variables=None):
        content = {"params": params or {}}
        if category == "variables":
            content["variables"] = variables or {}
        else:
            content["steps"] = steps or []
        self.lib.create(self.pid, {"key": key, "name": key,
                                   "category": category, "tags": [],
                                   "content": content})


class TestAssetCRUD(_Base):
    def test_create_and_versions_immutable(self):
        self.create("a", "assertion", params={"code": 200}, steps=[
            {"action": "assert", "type": "status",
             "actual": "${resp.status}", "expected": "${code}"}])
        v1 = self.lib.resolve(self.pid, "a", PIN_LATEST)
        self.assertEqual(v1["version"], 1)

        self.lib.publish(self.pid, "a", {"changelog": "v2", "content": {
            "params": {"code": 201},
            "steps": [{"action": "assert", "type": "status",
                       "actual": "${resp.status}", "expected": "${code}"}]}})
        self.assertEqual(self.lib.resolve(self.pid, "a", PIN_LATEST)["version"], 2)
        # 锁定 v1 的引用解析到的仍是不可变的旧版本
        self.assertEqual(self.lib.resolve(self.pid, "a", "1")["version"], 1)
        self.assertEqual(self.lib.resolve(self.pid, "a", 1)["params"]["code"], 200)

    def test_invalid_key_and_duplicate(self):
        self.create("flow_x", "step_flow", steps=[{"action": "set", "key": "k", "value": 1}])
        with self.assertRaises(AssetError):
            self.create("1bad", "step_flow", steps=[{"action": "set", "key": "k", "value": 1}])
        with self.assertRaises(AssetError):
            self.create("flow_x", "step_flow", steps=[{"action": "set", "key": "k", "value": 1}])

    def test_assertion_only_allows_assert_and_use(self):
        with self.assertRaises(AssetError):
            self.create("bad", "assertion",
                        steps=[{"action": "request", "method": "GET", "url": "/x"}])
        # assert + use 合法
        self.create("ok_assert", "assertion", params={"code": 200}, steps=[
            {"action": "assert", "type": "status", "actual": "${resp.status}",
             "expected": "${code}"}])
        self.create("wrap", "assertion", steps=[
            {"action": "use", "asset": "ok_assert", "version": "latest"}])

    def test_variables_template_requires_variables(self):
        with self.assertRaises(AssetError):
            self.create("v", "variables", variables={})

    def test_missing_asset_and_version(self):
        with self.assertRaises(AssetError):
            self.ex.expand(self.pid, [{"action": "use", "asset": "nope"}])
        self.create("a", "step_flow", steps=[{"action": "set", "key": "k", "value": 1}])
        with self.assertRaises(AssetError):
            self.ex.expand(self.pid, [{"action": "use", "asset": "a", "version": 9}])


class TestExpansion(_Base):
    def test_params_get_isolated_scopes(self):
        # 同一资产被嵌套两层、且形参同名 code，展开后不能互相覆盖
        self.create("ok", "assertion", params={"code": 200}, steps=[
            {"action": "assert", "type": "status",
             "actual": "${resp.status}", "expected": "${code}"}])
        self.create("outer", "step_flow", params={"code": 1}, steps=[
            {"action": "use", "asset": "ok", "params": {"code": "${code}"}},
            {"action": "use", "asset": "ok", "params": {"code": 500}}])
        r = self.ex.expand(self.pid, [
            {"action": "use", "asset": "outer"}])
        scoped = [s for s in r["steps"] if s["action"] == "set"
                  and s["key"].startswith("__")]
        keys = {s["key"] for s in scoped}
        # outer 的 code、两次内层 code 各自独立
        self.assertEqual(len(keys), 3)
        asserts = [s for s in r["steps"] if s["action"] == "assert"]
        expected_vars = {a["expected"][2:-1] for a in asserts}
        self.assertEqual(expected_vars, keys - {"__f1_outer_code"})
        self.assertNotEqual(asserts[0]["expected"], asserts[1]["expected"])

    def test_env_vars_preserved_through_expansion(self):
        # 环境变量不在资产作用域内，展开后保持 ${USERNAME}，由执行期按环境解析
        self.create("login", "step_flow",
                    params={"user": "${USERNAME}"}, steps=[
                        {"action": "set", "key": "u", "value": "${user}"}])
        r = self.ex.expand(self.pid, [{"action": "use", "asset": "login"}])
        bind = next(s for s in r["steps"] if s["name"] == "[参数] user")
        self.assertEqual(bind["value"], "${USERNAME}")

    def test_same_asset_different_environments(self):
        dev = self.envm.create(self.pid, {"name": "dev", "variables": {"USERNAME": "alice"}})
        stg = self.envm.create(self.pid, {"name": "stg", "variables": {"USERNAME": "bob"}})
        self.create("login", "step_flow", params={"user": "${USERNAME}"}, steps=[
            {"action": "set", "key": "who", "value": "${user}"}])
        who = {}
        for env in (dev, stg):
            r = self.ex.expand(self.pid, [{"action": "use", "asset": "login"}])
            res = TestExecutor().execute_case(
                {"id": "c", "name": "t", "steps": r["steps"]},
                self.envm.to_executor_config(env["id"]), timeout=10)
            who[env["name"]] = res["status"]
            set_who = next(s for s in res["steps"] if s["action"] == "set"
                           and s.get("message", "").startswith("设置 who"))
            who[env["name"] + "_val"] = set_who["message"]
        self.assertEqual(who["dev"], "passed")
        self.assertIn("alice", who["dev_val"])
        self.assertIn("bob", who["stg_val"])

    def test_variables_template_feeds_nested_asset(self):
        self.create("vars", "variables", variables={"account": "${USERNAME}"})
        self.create("login", "step_flow", params={"user": "x"}, steps=[
            {"action": "set", "key": "who", "value": "${user}"}])
        env = self.envm.create(self.pid, {"name": "dev", "variables": {"USERNAME": "alice"}})
        r = self.ex.expand(self.pid, [
            {"action": "use", "asset": "vars"},
            {"action": "use", "asset": "login", "params": {"user": "${account}"}}])
        res = TestExecutor().execute_case(
            {"id": "c", "name": "t", "steps": r["steps"]},
            self.envm.to_executor_config(env["id"]), timeout=10)
        msg = " ".join(s["message"] for s in res["steps"])
        self.assertIn("alice", msg)

    def test_cycle_detection(self):
        self.create("a1", "step_flow", steps=[
            {"action": "use", "asset": "a2", "version": "latest"}])
        self.create("a2", "step_flow", steps=[
            {"action": "use", "asset": "a1", "version": "latest"}])
        with self.assertRaises(AssetError):
            self.ex.expand(self.pid, [{"action": "use", "asset": "a1"}])

    def test_same_asset_used_twice_is_not_a_cycle(self):
        # 同一个版本被连续引用两次是合法复用，不是循环
        self.create("leaf", "step_flow", steps=[
            {"action": "set", "key": "k", "value": 1}])
        self.create("twice", "step_flow", steps=[
            {"action": "use", "asset": "leaf"},
            {"action": "use", "asset": "leaf"}])
        r = self.ex.expand(self.pid, [{"action": "use", "asset": "twice"}])
        self.assertEqual(len([s for s in r["steps"] if s["action"] == "set"]), 2)

    def test_origin_annotation(self):
        self.create("leaf", "step_flow", steps=[
            {"action": "set", "key": "k", "value": 1}])
        self.create("root", "step_flow", steps=[
            {"action": "use", "asset": "leaf"}])
        r = self.ex.expand(self.pid, [{"action": "use", "asset": "root"}],
                           annotate=True)
        inner = next(s for s in r["steps"] if s.get("key") == "k")
        keys = [o["key"] for o in inner["_origin"]]
        self.assertEqual(keys, ["root", "leaf"])


class TestImpactAndUpgrade(_Base):
    def setUp(self):
        super().setUp()
        # leaf(assert) <- mid(flow, latest 引用 leaf) <- 用例
        self.create("leaf", "assertion", params={"code": 200}, steps=[
            {"action": "assert", "type": "status",
             "actual": "${resp.status}", "expected": "${code}"}])
        self.create("mid", "step_flow", steps=[
            {"action": "use", "asset": "leaf", "version": "latest",
             "params": {"code": 200}}])
        self.cases = self.registry.store("cases")
        self.case_follow = self.cases.insert({"project_id": self.pid, "name": "cf",
            "steps": [{"action": "use", "asset": "mid", "version": "latest"}]})
        self.case_pin = self.cases.insert({"project_id": self.pid, "name": "cp",
            "steps": [{"action": "use", "asset": "leaf", "version": "1"}]})
        self.registry.store("suites").insert({"project_id": self.pid, "name": "S",
            "case_ids": [self.case_follow, self.case_pin]})

    def _publish_leaf_v2(self):
        self.lib.publish(self.pid, "leaf", {"changelog": "v2", "content": {
            "params": {"code": 201},
            "steps": [{"action": "assert", "type": "status",
                       "actual": "${resp.status}", "expected": "${code}"}]}})

    def test_impact_distinguishes_auto_and_locked(self):
        self._publish_leaf_v2()
        imp = self.lib.impact(self.pid, "leaf")
        auto = {a["key"] for a in imp["auto_assets"]}
        self.assertIn("mid", auto)
        self.assertEqual({c["case_id"] for c in imp["cases_auto"]},
                         {self.case_follow})
        locked_ids = {c["case_id"] for c in imp["cases_locked"]}
        self.assertEqual(locked_ids, {self.case_pin})
        suites = {s["suite_id"] for s in imp["suites"]}
        self.assertEqual(len(suites), 1)

    def test_following_latest_expands_to_new_version(self):
        self._publish_leaf_v2()
        r = self.ex.expand(self.pid, [{"action": "use", "asset": "mid",
                                       "version": "latest"}])
        used = {u["key"]: u["version"] for u in r["used"]}
        self.assertEqual(used["leaf"], 2)
        # 锁定的用例仍然拿到 v1
        r1 = self.ex.expand(self.pid, [{"action": "use", "asset": "leaf",
                                        "version": "1"}])
        self.assertEqual(r1["used"][0]["version"], 1)

    def test_outdated_and_batch_upgrade_cases(self):
        self._publish_leaf_v2()
        items = self.lib.outdated(self.pid)["items"]
        self.assertTrue(any(i["id"] == self.case_pin and i["pinned"] == "1"
                            and i["latest"] == 2 for i in items))
        r = self.lib.batch_upgrade(self.pid)
        upgraded_cases = {u["id"] for u in r["updated_cases"]}
        self.assertIn(self.case_pin, upgraded_cases)
        # 升级后用例步骤变成 latest
        case = self.cases.get(self.case_pin)
        self.assertEqual(case["steps"][0]["version"], PIN_LATEST)
        self.assertEqual(self.lib.outdated(self.pid)["count"], 0)

    def test_batch_upgrade_asset_republishes(self):
        # mid 的 latest 里锁着 leaf v1 -> 全量升级应为 mid 发布新版本
        # 先把 mid 改成锁定 leaf v1
        mid = self.lib.get_by_key(self.pid, "mid")
        latest = self.lib.get_version(mid["latest_version_id"])
        self.lib.publish(self.pid, "mid", {"changelog": "锁 leaf v1", "content": {
            "params": {},
            "steps": [{"action": "use", "asset": "leaf", "version": "1",
                       "params": {"code": 200}}]}})
        self._publish_leaf_v2()
        r = self.lib.batch_upgrade(self.pid)
        republished = {v["key"] for v in r["updated_versions"]}
        self.assertIn("mid", republished)
        new_mid = self.lib.resolve(self.pid, "mid", PIN_LATEST)
        self.assertEqual(new_mid["steps"][0]["version"], PIN_LATEST)

    def test_delete_blocked_when_referenced(self):
        with self.assertRaises(AssetError):
            self.lib.delete(self.pid, "leaf")
        r = self.lib.delete(self.pid, "leaf", force=True)
        self.assertTrue(r["ok"])
        self.assertIsNone(self.lib.get_by_key(self.pid, "leaf"))


class TestHelpers(unittest.TestCase):
    def test_rewrite_only_scope_vars(self):
        scope = {"code": "__a1_code"}
        self.assertEqual(_rewrite_vars("${code}", scope), "${__a1_code}")
        self.assertEqual(_rewrite_vars("${code}.x", scope), "${__a1_code}.x")
        # 点路径只替换根标识符
        self.assertEqual(_rewrite_vars("${code.detail}", scope),
                         "${__a1_code.detail}")
        # 不在作用域里的（如环境变量、resp）原样保留
        self.assertEqual(_rewrite_vars("${resp.status}", scope), "${resp.status}")
        self.assertEqual(_rewrite_vars("${USERNAME}", scope), "${USERNAME}")

    def test_substitute_env_vars(self):
        missing = set()
        out = substitute_env_vars("${BASE_URL}/login?r=${REGION}",
                                  {"BASE_URL": "http://dev", "REGION": "dev"},
                                  missing)
        self.assertEqual(out, "http://dev/login?r=dev")
        out2 = substitute_env_vars("${MISSING}/x", {"BASE_URL": "x"}, missing)
        self.assertEqual(out2, "${MISSING}/x")
        self.assertIn("MISSING", missing)


if __name__ == "__main__":
    unittest.main()
