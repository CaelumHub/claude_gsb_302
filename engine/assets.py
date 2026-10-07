"""可复用测试资产库：分类资产 + 版本 + 多层引用 + 影响分析 + 环境展开。

核心概念
--------
- **资产（asset）**：一个具名的可复用片段，按 ``key`` 在项目内唯一引用，
  分类（``category``）有三种：

  - ``step_flow``   操作步骤流（若干 request/set/script/... 步骤）
  - ``assertion``   断言片段（若干 assert 步骤）
  - ``variables``   变量模板（一组预置变量，展开成对应的 ``set`` 步骤）

- **资产版本（asset_version）**：资产的每次发布生成一个不可变版本
  （自增版本号 1、2、3…）。资产实体本身只存元数据 + 当前最新版本 id。

- **引用方式**：用例步骤（或另一个资产的步骤）里可以放一种特殊步骤

  .. code-block:: json

      {"action": "use", "asset": "flow_login", "version": "latest",
       "params": {"username": "${USERNAME}"}}

  ``version`` 取 ``"latest"`` 表示自动跟随最新版；取具体版本号（如 ``"1"``）
  表示锁定旧版。资产引用资产、用例引用多条资产，形成多层引用图。

- **展开（expand）**：执行 / 预览前把所有 ``use`` 步骤递归内联成具体步骤。
  资产声明的形参（``params``）会被提升为带唯一前缀的作用域变量，避免多层
  嵌套时同名参数互相覆盖；环境变量（如 ``${BASE_URL}``）不在资产作用域内，
  保持原样交给执行器按环境解析。

- **影响分析**：一条底层资产发布新版（或删除）后，沿引用图反向追溯哪些
  资产版本、用例、套件会受影响，并区分「自动跟随（立刻受影响）」与
  「锁定旧版（需主动升级）」。
"""

from __future__ import annotations

import re
import time
from typing import Any, Optional

from .models import new_id


# 资产分类
ASSET_CATEGORIES = ["step_flow", "assertion", "variables"]

CATEGORY_LABELS = {
    "step_flow": "操作步骤流",
    "assertion": "断言片段",
    "variables": "变量模板",
}

# 引用版本模式：跟随最新 / 锁定具体版本
PIN_LATEST = "latest"

KEY_PATTERN = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
IDENT_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_.]*)\}")

# 展开时允许的最大嵌套层数，防止失控引用占满资源
MAX_EXPAND_DEPTH = 32


class AssetError(ValueError):
    """资产配置 / 引用上的预期内错误。"""


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------

def _is_identifier(name: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name or ""))


def _validate_key(key: str) -> str:
    key = (key or "").strip()
    if not KEY_PATTERN.fullmatch(key):
        raise AssetError("资产 key 只能包含小写字母、数字、下划线，且以字母开头（最长 64）")
    return key


def _normalize_spec(spec: Any) -> str:
    """把引用方写的版本号规范成 ``"latest"`` 或整数字符串。"""
    if spec is None or spec == "":
        return PIN_LATEST
    if isinstance(spec, str) and spec.strip().lower() == PIN_LATEST:
        return PIN_LATEST
    try:
        n = int(spec)
    except (TypeError, ValueError):
        raise AssetError(f"无法识别的版本号: {spec!r}（应为正整数或 'latest'）")
    if n < 1:
        raise AssetError("版本号必须 >= 1")
    return str(n)


def _rewrite_vars(value: Any, scope: dict[str, str]) -> Any:
    """把字符串里命中当前作用域形参的 ``${p}`` 改写成带唯一前缀的变量名。

    环境变量、上游步骤产物（如 ``resp``）等不在 ``scope`` 里的标识符原样
    保留——这正是「同一资产在不同环境取不同值」的关键：环境变量延迟到执行
    期由执行器按环境注入解析。
    """
    if not isinstance(value, str) or not scope:
        return value

    def _sub(m: re.Match) -> str:
        expr = m.group(1)
        base, _, rest = expr.partition(".")
        target = scope.get(base)
        if target is None:
            return m.group(0)
        return "${" + target + (("." + rest) if rest else "") + "}"

    return IDENT_PATTERN.sub(_sub, value)


def substitute_env_vars(value: Any, env_vars: dict[str, Any],
                        missing: Optional[set] = None) -> Any:
    """预览用：把 ``${ENV}`` 用指定环境的变量值替换；缺失的变量原样保留。

    与执行器的 :func:`engine.executor.resolve_expr` 不同，这里只做展示层的
    文本展开（整体引用也转成字符串），并在 ``missing`` 里收集环境未提供的
    变量名，供预览页面标注。
    """
    if not isinstance(value, str):
        return value

    def _sub(m: re.Match) -> str:
        expr = m.group(1)
        base, _, rest = expr.partition(".")
        if base not in env_vars:
            if missing is not None:
                missing.add(base)
            return m.group(0)
        cur: Any = env_vars[base]
        if rest:
            for part in rest.split("."):
                if isinstance(cur, dict):
                    cur = cur.get(part)
                else:
                    cur = None
                if cur is None:
                    break
        return "" if cur is None else str(cur)

    return IDENT_PATTERN.sub(_sub, value)


# ---------------------------------------------------------------------------
# 资产库
# ---------------------------------------------------------------------------

class AssetLibrary:
    """资产 + 资产版本的领域服务，包裹两个分片存储。"""

    def __init__(self, registry):
        self.registry = registry
        self.assets = registry.store("assets")
        self.versions = registry.store("asset_versions")

    # -- 查询 -------------------------------------------------------------
    def list(self, project_id: str, category: Optional[str] = None,
             tag: Optional[str] = None, q: Optional[str] = None) -> list[dict]:
        where = [("project_id", "eq", project_id)]
        if category:
            where.append(("category", "eq", category))
        if tag:
            where.append(("tags", "contains", tag))
        rows = self.assets.query(where=where, order_by="created_at", order="asc")
        if q:
            needle = q.lower()
            rows = [a for a in rows
                    if needle in (a.get("name", "") + a.get("key", "")
                                  + a.get("description", "")).lower()]
        out = []
        for a in rows:
            latest = self.get_version(a["latest_version_id"]) if a.get("latest_version_id") else None
            item = dict(a)
            item["latest_version"] = latest.get("version") if latest else None
            item["changelog"] = latest.get("changelog", "") if latest else ""
            out.append(item)
        return out

    def brief(self, project_id: str) -> list[dict]:
        """轻量清单（用例编辑器插入引用时用）：key / 名称 / 分类 / 最新版。"""
        return [{"key": a["key"], "name": a["name"], "category": a["category"],
                 "latest_version": self.get_version(a["latest_version_id"]).get("version")
                 if a.get("latest_version_id") else None}
                for a in self.list(project_id)]

    def get_by_key(self, project_id: str, key: str) -> Optional[dict]:
        rows = self.assets.query(where=[("project_id", "eq", project_id),
                                        ("key", "eq", key)])
        return rows[0] if rows else None

    def get_version(self, version_id: str) -> Optional[dict]:
        return self.versions.get(version_id)

    def list_versions(self, project_id: str, key: str) -> list[dict]:
        asset = self.get_by_key(project_id, key)
        if asset is None:
            raise AssetError("资产不存在")
        rows = self.versions.query(
            where=[("asset_id", "eq", asset["id"])],
            order_by="version", order="desc")
        return rows

    def resolve(self, project_id: str, key: str, spec: Any) -> dict:
        """按引用方写的版本定位到一个**不可变**的资产版本。"""
        norm = _normalize_spec(spec)
        asset = self.get_by_key(project_id, key)
        if asset is None:
            raise AssetError(f"引用的资产不存在: {key}")
        if norm == PIN_LATEST:
            version = self.get_version(asset.get("latest_version_id"))
            if version is None:
                raise AssetError(f"资产 {key} 还没有发布任何版本")
            return version
        rows = self.versions.query(where=[("asset_id", "eq", asset["id"]),
                                          ("version", "eq", int(norm))])
        if not rows:
            raise AssetError(f"资产 {key} 的版本 {norm} 不存在")
        return rows[0]

    # -- 写入 -------------------------------------------------------------
    def create(self, project_id: str, payload: dict) -> dict:
        """新建资产并发布第 1 个版本。"""
        name = (payload.get("name") or "").strip()
        if not name:
            raise AssetError("资产名称不能为空")
        key = _validate_key(payload.get("key") or _suggest_key(name))
        category = payload.get("category", "step_flow")
        if category not in ASSET_CATEGORIES:
            raise AssetError(f"未知资产分类: {category}")
        if self.get_by_key(project_id, key) is not None:
            raise AssetError(f"资产 key 已存在: {key}")

        content = self._validate_content(category, payload.get("content"))
        now = time.time()
        asset = {
            "id": new_id("ast"),
            "project_id": project_id,
            "key": key,
            "name": name,
            "description": payload.get("description", ""),
            "category": category,
            "tags": payload.get("tags") or [],
            "latest_version_id": None,
            "version_count": 0,
            "created_at": now,
            "updated_at": now,
        }
        self.assets.insert(asset)
        version = self._publish(asset, content,
                                changelog=payload.get("changelog", "初始版本"))
        return asset

    def update_meta(self, project_id: str, key: str, patch: dict) -> dict:
        asset = self.get_by_key(project_id, key)
        if asset is None:
            raise AssetError("资产不存在")
        allowed = {k: patch[k] for k in ("name", "description", "tags") if k in patch}
        allowed["updated_at"] = time.time()
        return self.assets.update(asset["id"], allowed)

    def publish(self, project_id: str, key: str, payload: dict) -> dict:
        """给已有资产发布一个新版本（旧版本保持不可变）。"""
        asset = self.get_by_key(project_id, key)
        if asset is None:
            raise AssetError("资产不存在")
        content = self._validate_content(asset["category"], payload.get("content"))
        return self._publish(asset, content,
                             changelog=payload.get("changelog", ""))

    def _publish(self, asset: dict, content: dict, changelog: str) -> dict:
        """插入新版本并推进资产的最新版本指针（调用方需已校验 content）。"""
        version_no = int(asset.get("version_count", 0)) + 1
        version = {
            "id": new_id("astv"),
            "asset_id": asset["id"],
            "project_id": asset["project_id"],
            "key": asset["key"],
            "name": asset["name"],
            "category": asset["category"],
            "version": version_no,
            "changelog": changelog or f"v{version_no}",
            "params": content["params"],
            "variables": content["variables"],
            "steps": content["steps"],
            "published_at": time.time(),
        }
        self.versions.insert(version)
        self.assets.update(asset["id"], {
            "latest_version_id": version["id"],
            "version_count": version_no,
            "updated_at": time.time(),
        })
        return version

    def delete(self, project_id: str, key: str, force: bool = False) -> dict:
        """删除资产及其全部版本。

        默认情况下只要项目内还有任何引用（锁定或跟随）就拒绝，避免悬空引用；
        ``force=True`` 时连同版本一起删除，引用方将在展开期收到明确报错。
        """
        asset = self.get_by_key(project_id, key)
        if asset is None:
            raise AssetError("资产不存在")
        refs = self.references(project_id, key)
        total_refs = (len(refs["asset_versions"]) + len(refs["cases"])
                      + len(refs["suites"]))
        if total_refs and not force:
            raise AssetError(
                f"资产 {key} 仍被 {total_refs} 处引用"
                f"（资产 {len(refs['asset_versions'])} / 用例 {len(refs['cases'])}），"
                "请先解除引用或使用 force=1 强制删除")
        for v in self.versions.query(where=[("asset_id", "eq", asset["id"])]):
            self.versions.delete(v["id"])
        self.assets.delete(asset["id"])
        return {"ok": True, "deleted_versions": asset.get("version_count", 0)}

    # -- 内容校验 ----------------------------------------------------------
    def _validate_content(self, category: str, content: Any) -> dict:
        if not isinstance(content, dict):
            raise AssetError("content 必须是对象")
        params = content.get("params") or {}
        if not isinstance(params, dict):
            raise AssetError("params 必须是 {形参名: 默认表达式} 对象")
        for pname in params:
            if not _is_identifier(pname):
                raise AssetError(f"形参名不合法: {pname!r}")

        variables = content.get("variables") or {}
        steps = content.get("steps") or []

        if category == "variables":
            if not isinstance(variables, dict) or not variables:
                raise AssetError("变量模板必须提供非空 variables 对象")
            for vname in variables:
                if not _is_identifier(vname):
                    raise AssetError(f"变量名不合法: {vname!r}")
            steps = []
        else:
            if not isinstance(steps, list) or not steps:
                raise AssetError("步骤流 / 断言片段必须提供非空 steps 数组")
            for i, step in enumerate(steps):
                self._validate_step(step, i)
            if category == "assertion":
                bad = [i for i, s in enumerate(steps)
                       if s.get("action") not in ("assert", "use")]
                if bad:
                    raise AssetError(
                        f"断言片段只能包含 assert / use 步骤，第 {[i + 1 for i in bad]} 步不合规")
            variables = {}
        return {"params": params, "variables": variables, "steps": steps}

    def _validate_step(self, step: Any, index: int) -> None:
        if not isinstance(step, dict):
            raise AssetError(f"第 {index + 1} 步必须是对象")
        action = step.get("action")
        if action == "use":
            key = step.get("asset")
            if not key:
                raise AssetError(f"第 {index + 1} 步缺少 asset（引用的资产 key）")
            _normalize_spec(step.get("version"))
            if step.get("params") is not None and not isinstance(step.get("params"), dict):
                raise AssetError(f"第 {index + 1} 步 params 必须是对象")
        elif not action:
            raise AssetError(f"第 {index + 1} 步缺少 action")

    # -- 项目快照（影响分析 / 升级批处理用） -------------------------------
    def _scan_project(self, project_id: str) -> dict:
        assets = self.assets.query(where=[("project_id", "eq", project_id)])
        versions = self.versions.query(where=[("project_id", "eq", project_id)])
        cases = self.registry.store("cases").query(
            where=[("project_id", "eq", project_id)])
        suites = self.registry.store("suites").query(
            where=[("project_id", "eq", project_id)])
        return {"assets": assets, "versions": versions,
                "cases": cases, "suites": suites}

    @staticmethod
    def _refs_in_steps(steps: list) -> list[dict]:
        """提取一组步骤里的全部 ``use`` 引用（去重保序）。"""
        out, seen = [], set()
        for step in steps or []:
            if not isinstance(step, dict) or step.get("action") != "use":
                continue
            key = step.get("asset")
            spec = _normalize_spec(step.get("version"))
            sig = (key, spec)
            if sig not in seen:
                seen.add(sig)
                out.append({"asset": key, "version": spec})
        return out

    def references(self, project_id: str, key: str) -> dict:
        """谁直接引用了指定资产（任何版本引用都算，删除保护用）。"""
        scan = self._scan_project(project_id)
        asset_versions, cases = [], []
        for v in scan["versions"]:
            if any(r["asset"] == key for r in self._refs_in_steps(v.get("steps"))):
                asset_versions.append({"version_id": v["id"], "asset_key": v["key"],
                                       "name": v["name"], "version": v["version"]})
        for c in scan["cases"]:
            if any(r["asset"] == key for r in self._refs_in_steps(c.get("steps"))):
                cases.append({"case_id": c["id"], "name": c["name"]})
        suite_ids = {c["id"] for c in scan["cases"]
                     if any(r["asset"] == key for r in self._refs_in_steps(c.get("steps")))}
        suites = [{"suite_id": s["id"], "name": s["name"]}
                  for s in scan["suites"]
                  if suite_ids.intersection(s.get("case_ids") or [])]
        return {"asset_versions": asset_versions, "cases": cases, "suites": suites}

    def impact(self, project_id: str, key: str) -> dict:
        """影响分析：最新版改动沿引用链反向扩散到的全部资产 / 用例 / 套件。

        - ``auto_*``：引用写的是 ``latest``，新版一生效立即受影响；
        - ``locked_*``：锁定具体版本的引用，旧版不可变所以**当前不受影响**，
          但作为「需要评估升级」的候选项一并列出。
        """
        target = self.get_by_key(project_id, key)
        if target is None:
            raise AssetError("资产不存在")
        scan = self._scan_project(project_id)

        latest_ids = {a["latest_version_id"]: a for a in scan["assets"]}
        # version_id -> 版本（含其 use 引用）
        ver_index = {v["id"]: v for v in scan["versions"]}

        # 反向边：被引用资产 key -> 引用它的版本 id（仅当引用指向「当前最新版」）
        reverse_latest: dict[str, set[str]] = {}
        # 反向边：任意版本引用（用于把锁定者也找出来）
        reverse_any: dict[str, set[str]] = {}
        locked_hits: dict[str, list[dict]] = {}
        for v in scan["versions"]:
            for ref in self._refs_in_steps(v.get("steps")):
                reverse_any.setdefault(ref["asset"], set()).add(v["id"])
                if ref["version"] == PIN_LATEST:
                    reverse_latest.setdefault(ref["asset"], set()).add(v["id"])
                else:
                    locked_hits.setdefault(ref["asset"], []).append(
                        {"version_id": v["id"], "asset_key": v["key"],
                         "name": v["name"], "version": v["version"],
                         "pinned": ref["version"]})

        # BFS：只沿 latest 边扩散——这才是「立刻生效」的影响
        auto_version_ids: set[str] = set()
        frontier = set(reverse_latest.get(key, set()))
        while frontier:
            vid = frontier.pop()
            if vid in auto_version_ids:
                continue
            auto_version_ids.add(vid)
            v = ver_index.get(vid)
            if v:
                frontier |= reverse_latest.get(v["key"], set()) - auto_version_ids

        auto_keys = {ver_index[vid]["key"] for vid in auto_version_ids}
        affected_keys = auto_keys | {key}

        # 锁定者：任何引用了受影响资产、但写死版本号的版本（排除已自动跟随的）
        locked_assets = []
        for akey in affected_keys:
            for hit in locked_hits.get(akey, []):
                if hit["version_id"] not in auto_version_ids:
                    locked_assets.append({**hit, "target_asset": akey})

        # 用例：读它们步骤里的引用，区分跟随 / 锁定
        cases_auto, cases_locked = [], []
        affected_case_ids: set[str] = set()
        for c in scan["cases"]:
            refs = self._refs_in_steps(c.get("steps"))
            hit_keys_auto = {r["asset"] for r in refs
                             if r["version"] == PIN_LATEST and r["asset"] in affected_keys}
            hit_locked = [r for r in refs
                          if r["version"] != PIN_LATEST and r["asset"] in affected_keys]
            if hit_keys_auto:
                cases_auto.append({"case_id": c["id"], "name": c["name"],
                                   "priority": c.get("priority"),
                                   "via": sorted(hit_keys_auto)})
                affected_case_ids.add(c["id"])
            if hit_locked:
                cases_locked.append({"case_id": c["id"], "name": c["name"],
                                     "priority": c.get("priority"),
                                     "pinned": [{"asset": r["asset"],
                                                 "version": r["version"]}
                                                for r in hit_locked]})
                affected_case_ids.add(c["id"])

        suites = [{"suite_id": s["id"], "name": s["name"],
                   "affected_cases": sorted(set(s.get("case_ids") or [])
                                            & affected_case_ids)}
                  for s in scan["suites"]
                  if set(s.get("case_ids") or []) & affected_case_ids]

        return {
            "target": {"key": key, "name": target["name"],
                       "category": target["category"],
                       "latest_version_id": target.get("latest_version_id"),
                       "version_count": target.get("version_count", 0)},
            "auto_assets": [
                {"key": ver_index[vid]["key"], "name": ver_index[vid]["name"],
                 "version_id": vid,
                 "version": ver_index[vid]["version"]}
                for vid in sorted(auto_version_ids,
                                  key=lambda x: ver_index[x]["published_at"])],
            "locked_assets": locked_assets,
            "cases_auto": cases_auto,
            "cases_locked": cases_locked,
            "suites": suites,
        }

    # -- 过期引用 & 批量升级 -----------------------------------------------
    def outdated(self, project_id: str) -> dict:
        """列出所有「锁定版本落后于最新版」的引用点。"""
        scan = self._scan_project(project_id)
        latest: dict[str, int] = {a["key"]: self.get_version(a["latest_version_id"])["version"]
                                  for a in scan["assets"] if a.get("latest_version_id")}
        items: list[dict] = []
        for v in scan["versions"]:
            for ref in self._refs_in_steps(v.get("steps")):
                lv = latest.get(ref["asset"])
                if ref["version"] != PIN_LATEST and lv and int(ref["version"]) < lv:
                    items.append({"ref_type": "asset_version", "id": v["id"],
                                  "asset_key": v["key"], "asset_name": v["name"],
                                  "ref": ref["asset"], "pinned": ref["version"],
                                  "latest": lv})
        for c in scan["cases"]:
            for ref in self._refs_in_steps(c.get("steps")):
                lv = latest.get(ref["asset"])
                if ref["version"] != PIN_LATEST and lv and int(ref["version"]) < lv:
                    items.append({"ref_type": "case", "id": c["id"],
                                  "asset_key": None, "asset_name": c["name"],
                                  "ref": ref["asset"], "pinned": ref["version"],
                                  "latest": lv})
        return {"items": items,
                "count": len(items),
                "assets_affected": len({i["ref"] for i in items})}

    def batch_upgrade(self, project_id: str,
                      targets: Optional[list] = None) -> dict:
        """批量把锁定旧版的引用升到最新（或指定版本）。

        - 不带 ``targets``：升级项目内所有过期引用。

          * 用例里的锁定引用直接改写步骤（用例可变）；
          * 资产版本不可变——对「最新版里仍锁着旧版」的资产自动发布一个
            新版本承载升级后的引用，锁定该资产旧版的引用不会被动到。

        - 带 ``targets``：每项
          ``{"ref_type": "case"|"asset_version", "id", "asset"?, "to_version"?}``，
          只升级指定记录里的引用（``asset`` 缺省表示该记录里所有过期引用，
          ``to_version`` 缺省表示跟最新）；``asset_version`` 目标同样以
          发布新版本落地。
        """
        scan = self._scan_project(project_id)
        updated_cases, updated_versions, skipped = [], [], []

        if targets:
            for t in targets:
                ref_type, rid = t.get("ref_type"), t.get("id")
                if ref_type not in ("case", "asset_version") or not rid:
                    raise AssetError(f"非法升级目标: {t!r}")
                to_map = {t["asset"]: t["to_version"]} if t.get("asset") and t.get("to_version") else {}
                only = {t["asset"]} if t.get("asset") else None
                if ref_type == "case":
                    case = self.registry.store("cases").get(rid)
                    if case is None or case.get("project_id") != project_id:
                        skipped.append({"ref_type": ref_type, "id": rid, "reason": "用例不存在"})
                        continue
                    new_steps, changed = self._upgrade_steps(
                        [dict(s) for s in case.get("steps") or []], to_map, only)
                    if changed:
                        self.registry.store("cases").update(rid, {"steps": new_steps})
                        updated_cases.append({"id": rid, "upgraded": changed})
                    else:
                        skipped.append({"ref_type": ref_type, "id": rid,
                                        "reason": "没有可升级的锁定引用"})
                else:
                    ver = self.versions.get(rid)
                    asset = self.assets.get(ver["asset_id"]) if ver else None
                    if ver is None or asset is None or asset.get("project_id") != project_id:
                        skipped.append({"ref_type": ref_type, "id": rid, "reason": "资产版本不存在"})
                        continue
                    if ver["id"] != asset.get("latest_version_id"):
                        skipped.append({"ref_type": ref_type, "id": rid,
                                        "reason": "只能在最新版基础上升版，请先切换到最新版"})
                        continue
                    new_version = self._republish_with_upgrade(asset, to_map, only)
                    if new_version:
                        updated_versions.append({"id": new_version["id"], "key": new_version["key"],
                                                 "version": new_version["version"]})
                    else:
                        skipped.append({"ref_type": ref_type, "id": rid,
                                        "reason": "没有可升级的锁定引用"})
            return {"updated_cases": updated_cases,
                    "updated_versions": updated_versions,
                    "skipped": skipped}

        # 全量模式：先升级资产（发布新版本），再升级用例
        republished_keys: set[str] = set()
        for asset in scan["assets"]:
            latest = self.get_version(asset.get("latest_version_id"))
            if latest is None:
                continue
            if not any(r["version"] != PIN_LATEST for r in self._refs_in_steps(latest.get("steps"))):
                continue
            new_version = self._republish_with_upgrade(asset, {}, None)
            if new_version:
                republished_keys.add(asset["key"])
                updated_versions.append({"id": new_version["id"], "key": new_version["key"],
                                         "version": new_version["version"]})

        for c in scan["cases"]:
            new_steps, changed = self._upgrade_steps(
                [dict(s) for s in c.get("steps") or []], {}, None)
            if changed:
                self.registry.store("cases").update(c["id"], {"steps": new_steps})
                updated_cases.append({"id": c["id"], "upgraded": changed})

        return {"updated_cases": updated_cases,
                "updated_versions": updated_versions,
                "republished_assets": sorted(republished_keys),
                "skipped": skipped}

    def _republish_with_upgrade(self, asset: dict, to_map: dict,
                                only: Optional[set]) -> Optional[dict]:
        """以资产最新版为基础、升级其中的过期引用后发布一个新版本。"""
        latest = self.get_version(asset.get("latest_version_id"))
        new_steps, changed = self._upgrade_steps(
            [dict(s) for s in latest.get("steps") or []], to_map, only)
        if not changed:
            return None
        content = {"params": latest.get("params") or {},
                   "variables": latest.get("variables") or {},
                   "steps": new_steps}
        return self._publish(
            asset, content,
            changelog=f"批量升级引用: {', '.join(sorted(set(changed)))}")

    @staticmethod
    def _upgrade_steps(steps: list, to_map: dict,
                       only: Optional[set] = None) -> tuple[list, list]:
        """把步骤里过期的锁定引用升级。

        ``to_map`` 为 ``{asset: 版本}``（空表示一律跟最新）；
        ``only`` 非空时只处理集合内的资产。
        """
        changed: list[str] = []
        for step in steps:
            if not isinstance(step, dict) or step.get("action") != "use":
                continue
            key = step.get("asset")
            if only is not None and key not in only:
                continue
            spec = _normalize_spec(step.get("version"))
            if spec == PIN_LATEST:
                continue
            to_spec = _normalize_spec(to_map.get(key)) if key in to_map else PIN_LATEST
            if to_spec != spec:
                step["version"] = to_spec
                changed.append(key)
        return steps, changed


# ---------------------------------------------------------------------------
# 展开器：把 use 步骤递归内联成具体步骤
# ---------------------------------------------------------------------------

class AssetExpander:
    """把含 ``use`` 引用的步骤列表展开成执行器可直接执行的具体步骤。"""

    def __init__(self, registry):
        self.library = AssetLibrary(registry)

    def expand_case(self, case: dict) -> dict:
        return self.expand(case.get("project_id"), case.get("steps") or [],
                           annotate=False)

    def expand(self, project_id: str, steps: list,
               annotate: bool = False) -> dict:
        """展开一组步骤。

        返回 ``{"steps": [...], "used": [{key, version, name, category}],
        "missing_env": [...]}``；``annotate=True`` 时每个具体步骤附带
        ``_origin`` 来源链，供预览页面标注层级。
        """
        ctx = _ExpandContext(self.library, project_id, annotate=annotate)
        out = ctx.render_steps(steps, scope={}, chain=[])
        return {"steps": out, "used": ctx.used, "missing_env": sorted(ctx.missing_env)}


class _ExpandContext:
    """单次展开的可变上下文：已用资产去重、循环检测、环境变量收集。"""

    def __init__(self, library: AssetLibrary, project_id: str, annotate: bool):
        self.lib = library
        self.project_id = project_id
        self.annotate = annotate
        self.used: list[dict] = []
        self._used_sig: set = set()
        # 每次 use 调用一个递增帧号：即使同一版本被引用多次，形参作用域也相互隔离
        self._frame_seq = 0
        self.missing_env: set = set()

    def render_steps(self, steps: list, scope: dict, chain: list,
                     depth: int = 0) -> list:
        out: list[dict] = []
        for step in steps or []:
            if isinstance(step, dict) and step.get("action") == "use":
                out.extend(self._render_use(step, scope, chain, depth))
            else:
                out.append(self._concrete(step, scope, chain))
        return out

    def _concrete(self, step: dict, scope: dict, chain: list) -> dict:
        rendered: dict = {}
        for k, v in step.items():
            rendered[k] = self._rewrite_value(v, scope)
        if self.annotate and chain:
            rendered["_origin"] = list(chain)
        return rendered

    def _rewrite_value(self, value: Any, scope: dict) -> Any:
        if isinstance(value, str):
            rewritten = _rewrite_vars(value, scope)
            return rewritten
        if isinstance(value, dict):
            return {k: self._rewrite_value(v, scope) for k, v in value.items()}
        if isinstance(value, list):
            return [self._rewrite_value(v, scope) for v in value]
        return value

    def _render_use(self, use_step: dict, parent_scope: dict,
                    chain: list, depth: int) -> list:
        if depth >= MAX_EXPAND_DEPTH:
            raise AssetError(f"资产嵌套超过最大层数 {MAX_EXPAND_DEPTH}")
        key = use_step.get("asset")
        version = self.lib.resolve(self.project_id, key, use_step.get("version"))

        sig = (version["id"],)
        if sig in self._used_sig and any(c["version_id"] == version["id"] for c in chain):
            raise AssetError(
                f"检测到资产循环引用: {' -> '.join(c['key'] for c in chain)} -> {key}")
        if sig not in self._used_sig:
            self._used_sig.add(sig)
            self.used.append({"key": version["key"], "name": version["name"],
                              "category": version["category"],
                              "version": version["version"],
                              "version_id": version["id"]})

        origin = {"key": version["key"], "name": version["name"],
                  "version": version["version"], "version_id": version["id"],
                  "category": version["category"]}
        next_chain = chain + [origin]

        # 1) 形参绑定：调用方实参在「父作用域 + 当前变量表」里求值，
        #    绑定到带唯一前缀的本资产作用域变量，避免多层嵌套 / 同资产多次
        #    引用时同名参数互相覆盖。
        self._frame_seq += 1
        suffix = f"f{self._frame_seq}_{version['key']}"
        child_scope: dict[str, str] = {}
        bind_steps: list[dict] = []
        params = use_step.get("params") or {}
        for pname, default_expr in (version.get("params") or {}).items():
            scoped = f"__{suffix}_{pname}"
            child_scope[pname] = scoped
            raw_expr = params.get(pname, default_expr)
            expr = self._rewrite_value(raw_expr if raw_expr is not None else "",
                                       parent_scope)
            bind = {"action": "set", "name": f"[参数] {pname}",
                    "key": scoped, "value": expr}
            if self.annotate:
                bind["_origin"] = next_chain
            bind_steps.append(bind)

        # 2) 变量模板：每个预置变量展开为一个 set 步骤（值里允许写 ${ENV}）
        var_steps: list[dict] = []
        for vname, vval in (version.get("variables") or {}).items():
            vset = {"action": "set", "name": f"[变量模板] {vname}",
                    "key": vname,
                    "value": self._rewrite_value(vval, child_scope)}
            if self.annotate:
                vset["_origin"] = next_chain
            var_steps.append(vset)

        # 3) 递归内联资产自身步骤（形参按 child_scope 改写）
        body = self.render_steps(version.get("steps") or [], child_scope,
                                 next_chain, depth + 1)
        return bind_steps + var_steps + body
