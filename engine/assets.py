"""可复用资产库：操作步骤 / 断言片段 / 变量模板的版本化复用。

解决的问题
----------
同一段登录步骤在几十条用例里复制几十遍，改一处要逐条翻、漏一条就出错。
资产库把这类重复内容沉淀成**资产（asset）**，用例与资产都只保存引用，
执行 / 预览时再把引用递归展开为完整步骤，一处修改即可沿引用链生效。

数据模型（两类实体，均存于分片存储）
------------------------------------
- ``asset_families`` 资产族：一条逻辑资产（如「标准登录」），含分类、
  标签、当前最新版本号等元信息；
- ``asset_versions`` 资产版本：不可变的具体内容，属于某个资产族，
  版本号单调递增。内容统一是一组步骤（``steps``）——
  「操作步骤 / 断言片段 / 变量模板」按 ``category`` 区分语义，
  变量模板就是若干 ``set`` 步骤。

引用与版本跟随策略
------------------
引用方（用例步骤，或资产版本里的步骤）用一个特殊步骤表达引用::

    {"action": "use_asset", "asset_id": "ast_...",
     "version": 3, "pin": "lock" | "follow"}

- ``lock``（锁定）：永远使用引用时记下的 ``version``，资产升级不受影响；
- ``follow``（跟随）：每次展开都解析为资产族当前最新版本，资产一升级，
  所有跟随方自动获得新内容。

引用可以多层嵌套：资产引用资产、用例引用多条资产，:meth:`expand_steps`
递归把 ``use_asset`` 内联展开成被引用版本的步骤，因此改动一条底层资产
会沿引用链层层扩散到所有用例。环形引用会被检测并报错，不会无限递归。

环境变量
--------
变量模板里的值可以写成 ``${BASE_URL}``。展开时不做变量替换（变量替换
仍由执行器在运行时做），但 :meth:`preview_case` / ``expand_case`` 接受
环境变量表，预览里会把每个变量的**取值来源**（环境 / 资产默认 / 未定义）
标出来，做到「同一资产在不同环境下展开结果不同」且可见、可核对。

影响分析
--------
:meth:`impact_of` 沿「谁引用了我」反向遍历引用图，返回受影响的资产、
用例与套件，并区分每个引用方是锁定旧版（不受新版本影响）还是跟随
最新版（会立即受影响），供批量升级使用。
"""

from __future__ import annotations

import time
from typing import Any, Optional

from .models import new_id


# 资产分类
ASSET_CATEGORIES = ["step", "assertion", "variables"]

ASSET_CATEGORY_LABELS = {
    "step": "操作步骤",
    "assertion": "断言片段",
    "variables": "变量模板",
}

# 版本跟随策略
PIN_MODES = ["lock", "follow"]

# 用例 / 资产步骤中「引用一条资产」的动作名
USE_ASSET_ACTION = "use_asset"

# 递归展开的最大深度，防御异常数据造成的过深嵌套
MAX_EXPAND_DEPTH = 32


class AssetError(Exception):
    """资产库的预期内错误（不存在 / 版本不存在 / 环形引用等）。"""


class AssetLibrary:
    """版本化可复用资产库，包裹 ``asset_families`` / ``asset_versions`` 存储。"""

    def __init__(self, registry):
        self.registry = registry
        self._families = registry.store("asset_families")
        self._versions = registry.store("asset_versions")

    # ------------------------------------------------------------------ 资产族

    def list_families(self, project_id: str, *, q: str = "",
                      category: str = "", tag: str = "") -> list[dict]:
        """列出项目下的资产族（附带最新版本概要），支持搜索 / 分类 / 标签筛选。"""
        where = [("project_id", "eq", project_id)]
        if category:
            where.append(("category", "eq", category))
        if tag:
            where.append(("tags", "contains", tag))
        families = self._families.query(where=where,
                                        order_by="updated_at", order="desc")
        if q:
            needle = q.lower()
            families = [
                f for f in families
                if needle in (f.get("name", "") + f.get("description", "")
                              + " ".join(f.get("tags", []))).lower()
            ]
        out = []
        for f in families:
            f = dict(f)
            latest = self.get_version(f["id"], f.get("latest_version"))
            f["latest"] = self._version_summary(latest) if latest else None
            f["usage_count"] = self._count_usages(f["id"])
            out.append(f)
        return out

    def get_family(self, asset_id: str) -> Optional[dict]:
        return self._families.get(asset_id)

    def create_family(self, project_id: str, payload: dict) -> dict:
        """创建资产族并写入首个版本。"""
        name = (payload.get("name") or "").strip()
        if not name:
            raise AssetError("资产名称不能为空")
        category = payload.get("category", "step")
        if category not in ASSET_CATEGORIES:
            raise AssetError(f"未知资产分类: {category}")
        steps = payload.get("steps") or []
        if not isinstance(steps, list):
            raise AssetError("steps 必须是数组")
        self._validate_steps(steps)

        family = {
            "id": new_id("ast"),
            "project_id": project_id,
            "name": name,
            "description": payload.get("description", ""),
            "category": category,
            "tags": payload.get("tags") or [],
            "latest_version": 1,
            "created_at": time.time(),
            "updated_at": time.time(),
        }
        self._families.insert(family)
        self._insert_version(family, 1, steps,
                             payload.get("change_note", "初始版本"))
        return family

    def update_family_meta(self, asset_id: str, patch: dict) -> dict:
        """只改元信息（名称 / 描述 / 标签 / 分类），不动已有版本内容。"""
        family = self._families.get(asset_id)
        if family is None:
            raise AssetError("资产不存在")
        allowed = {k: patch[k] for k in
                   ("name", "description", "tags", "category") if k in patch}
        if "category" in allowed and allowed["category"] not in ASSET_CATEGORIES:
            raise AssetError(f"未知资产分类: {allowed['category']}")
        return self._families.update(asset_id, allowed)

    def delete_family(self, asset_id: str) -> bool:
        """删除资产族及其全部版本（引用方再展开时会得到「资产已删除」错误）。"""
        family = self._families.get(asset_id)
        if family is None:
            return False
        for v in self.list_versions(asset_id):
            self._versions.delete(v["id"])
        return self._families.delete(asset_id)

    # ------------------------------------------------------------------ 版本

    def list_versions(self, asset_id: str) -> list[dict]:
        rows = self._versions.query(where=[("asset_id", "eq", asset_id)],
                                    order_by="version", order="desc")
        return [self._version_summary(v, include_steps=True) for v in rows]

    def get_version(self, asset_id: str, version: int) -> Optional[dict]:
        if version is None:
            return None
        rows = self._versions.query(
            where=[("asset_id", "eq", asset_id), ("version", "eq", version)],
            limit=1)
        return rows[0] if rows else None

    def publish_version(self, asset_id: str, payload: dict) -> dict:
        """发布新版本：版本号 +1，旧版本原样保留（不可变）。"""
        family = self._families.get(asset_id)
        if family is None:
            raise AssetError("资产不存在")
        steps = payload.get("steps")
        if steps is None:
            # 未给内容则复制最新版本内容，便于「小改发新版」
            latest = self.get_version(asset_id, family["latest_version"])
            steps = [dict(s) for s in (latest or {}).get("steps", [])]
        if not isinstance(steps, list):
            raise AssetError("steps 必须是数组")
        self._validate_steps(steps, owner_family_id=asset_id)

        next_no = int(family["latest_version"]) + 1
        version = self._insert_version(family, next_no, steps,
                                       payload.get("change_note", ""))
        self._families.update(asset_id, {
            "latest_version": next_no, "updated_at": time.time()})
        return version

    def _insert_version(self, family: dict, no: int, steps: list,
                        change_note: str) -> dict:
        version = {
            "id": new_id("astv"),
            "asset_id": family["id"],
            "project_id": family["project_id"],
            "version": no,
            "steps": steps,
            "change_note": change_note or "",
            "created_at": time.time(),
        }
        self._versions.insert(version)
        return version

    @staticmethod
    def _version_summary(v: dict, include_steps: bool = False) -> dict:
        if v is None:
            return None
        out = {
            "id": v["id"], "asset_id": v["asset_id"],
            "version": v["version"], "change_note": v.get("change_note", ""),
            "step_count": len(v.get("steps") or []),
            "created_at": v.get("created_at"),
            "refs": AssetLibrary._refs_in_steps(v.get("steps") or []),
        }
        if include_steps:
            out["steps"] = v.get("steps") or []
        return out

    # ------------------------------------------------------------------ 校验 / 引用扫描

    def _validate_steps(self, steps: list, owner_family_id: Optional[str] = None) -> None:
        if not isinstance(steps, list):
            raise AssetError("steps 必须是数组")
        for i, step in enumerate(steps):
            if not isinstance(step, dict):
                raise AssetError(f"第 {i + 1} 步不是对象")
            if step.get("action") == USE_ASSET_ACTION:
                ref_id = step.get("asset_id")
                if not ref_id:
                    raise AssetError(f"第 {i + 1} 步缺少 asset_id")
                pin = step.get("pin", "follow")
                if pin not in PIN_MODES:
                    raise AssetError(f"第 {i + 1} 步的 pin 只能是 lock/follow")
                family = self._families.get(ref_id)
                if family is None:
                    raise AssetError(f"第 {i + 1} 步引用的资产 {ref_id} 不存在")
                if owner_family_id and ref_id == owner_family_id:
                    raise AssetError("资产不能直接引用自身")
                if pin == "lock":
                    target = int(step.get("version") or 0)
                    if not self.get_version(ref_id, target):
                        raise AssetError(
                            f"第 {i + 1} 步锁定的版本 {target} 不存在")

    @staticmethod
    def _refs_in_steps(steps: list) -> list[dict]:
        """抽出一组步骤里的全部资产引用。"""
        refs = []
        for s in steps or []:
            if isinstance(s, dict) and s.get("action") == USE_ASSET_ACTION:
                refs.append({
                    "asset_id": s.get("asset_id"),
                    "version": s.get("version"),
                    "pin": s.get("pin", "follow"),
                })
        return refs

    def _count_usages(self, asset_id: str) -> int:
        """被多少个资产版本 + 用例直接引用（列表页展示用）。"""
        n = 0
        for v in self._versions.all():
            if any(r["asset_id"] == asset_id
                   for r in self._refs_in_steps(v.get("steps") or [])):
                n += 1
        for c in self.registry.store("cases").all():
            if any(r["asset_id"] == asset_id
                   for r in self._refs_in_steps(c.get("steps") or [])):
                n += 1
        return n

    # ------------------------------------------------------------------ 展开（解析引用）

    def _resolve_ref(self, ref: dict) -> tuple[dict, dict]:
        """按跟随策略把一个引用解析成 (资产族, 具体版本)。"""
        family = self._families.get(ref.get("asset_id"))
        if family is None:
            raise AssetError(f"引用的资产 {ref.get('asset_id')} 不存在或已删除")
        pin = ref.get("pin", "follow")
        target_no = int(ref["version"]) if pin == "lock" \
            else int(family["latest_version"])
        version = self.get_version(family["id"], target_no)
        if version is None:
            raise AssetError(
                f"资产「{family['name']}」的版本 {target_no} 不存在")
        return family, version

    def expand_steps(self, steps: list, *, _chain: Optional[list] = None,
                     _depth: int = 0) -> list[dict]:
        """把步骤序列中的 ``use_asset`` 递归内联展开。

        展开后每个被内联的步骤带 ``_asset_path``（资产名/vN 面包屑）与
        ``_asset_ref``（来源资产 id + 版本），便于预览标注来源；普通步骤
        原样返回。变量替换不在此处发生——运行时仍由执行器处理。
        """
        chain = _chain or []
        if _depth > MAX_EXPAND_DEPTH:
            raise AssetError("资产引用嵌套过深")
        out: list[dict] = []
        for step in steps or []:
            if isinstance(step, dict) and step.get("action") == USE_ASSET_ACTION:
                family, version = self._resolve_ref(step)
                key = (family["id"], version["version"])
                if key in chain:
                    trail = " → ".join(
                        f"{aid} v{no}" for aid, no in chain + [key])
                    raise AssetError(f"检测到环形引用: {trail}")
                crumb = f"{family['name']}/v{version['version']}"
                for inner in version.get("steps") or []:
                    expanded = self.expand_steps(
                        [inner], _chain=chain + [key], _depth=_depth + 1)
                    for e in expanded:
                        e = dict(e)
                        parent = e.get("_asset_path")
                        e["_asset_path"] = (crumb + (" → " + parent if parent else ""))
                        e["_asset_ref"] = {"asset_id": family["id"],
                                           "version": version["version"],
                                           "pin": step.get("pin", "follow")}
                        out.append(e)
            else:
                out.append(step)
        return out

    def expand_case(self, case: dict) -> dict:
        """返回展开资产引用后的用例副本（供执行器使用）。"""
        expanded = dict(case)
        expanded["steps"] = self.expand_steps(case.get("steps") or [])
        return expanded

    # ------------------------------------------------------------------ 变量与环境预览

    @staticmethod
    def collect_variable_names(steps: list) -> list[str]:
        """收集一组步骤里需要外部提供的 ``${name}`` 输入变量名。

        - ``request``/``script`` 的 ``save_as`` 是步骤产出，不算输入；
        - ``set`` 的值若是字面量，key 是纯产出（不算输入）；但值里含
          ``${X}`` 时，X 仍是需要环境 / 上游提供的输入变量；
        - ``case`` 由执行器内置注入。
        """
        import re
        names: set[str] = set()
        produced: set[str] = {"case", "_assertions"}

        def refs_in(value) -> set[str]:
            found: set[str] = set()
            if isinstance(value, str):
                for m in re.findall(r"\$\{([A-Za-z_][A-Za-z0-9_.]*)\}", value):
                    found.add(m.split(".")[0])
            elif isinstance(value, dict):
                for v in value.values():
                    found |= refs_in(v)
            elif isinstance(value, list):
                for v in value:
                    found |= refs_in(v)
            return found

        for step in steps or []:
            if not isinstance(step, dict):
                continue
            if step.get("save_as"):
                produced.add(step["save_as"])
            if step.get("action") == "set" and step.get("key"):
                # 仅当值不引用其它变量时，该 key 才是「纯产出」
                if not refs_in(step.get("value")):
                    produced.add(step["key"])
            names |= refs_in(step)
        return sorted(n for n in names if n not in produced)

    def preview_case(self, case: dict, env_variables: Optional[dict] = None) -> dict:
        """按环境变量表展开用例，给出完整步骤与变量取值来源预览。"""
        env_variables = env_variables or {}
        steps = self.expand_steps(case.get("steps") or [])

        # 资产在展开链路中「set」的变量视为资产默认值
        defaults: dict[str, Any] = {}
        for s in steps:
            if isinstance(s, dict) and s.get("action") == "set" and s.get("key"):
                defaults.setdefault(s["key"], s.get("value"))

        variables = []
        for name in self.collect_variable_names(steps):
            if name in env_variables:
                source, value = "environment", env_variables[name]
            elif name in defaults:
                source, value = "asset_default", defaults[name]
            else:
                source, value = "undefined", None
            variables.append({"name": name, "source": source, "value": value})
        return {
            "case_id": case.get("id"),
            "case_name": case.get("name"),
            "steps": steps,
            "variables": variables,
            "step_count": len(steps),
            "asset_refs": self._refs_in_steps(case.get("steps") or []),
        }

    def preview_asset(self, asset_id: str, version: Optional[int] = None,
                      env_variables: Optional[dict] = None) -> dict:
        family = self._families.get(asset_id)
        if family is None:
            raise AssetError("资产不存在")
        version_no = version or family["latest_version"]
        ver = self.get_version(asset_id, version_no)
        if ver is None:
            raise AssetError(f"版本 {version_no} 不存在")
        fake_case = {"id": None, "name": family["name"],
                     "steps": ver.get("steps") or []}
        preview = self.preview_case(fake_case, env_variables)
        preview["asset_id"] = asset_id
        preview["version"] = version_no
        preview["family"] = {"id": family["id"], "name": family["name"],
                             "category": family["category"],
                             "latest_version": family["latest_version"]}
        return preview

    # ------------------------------------------------------------------ 影响分析 / 批量升级

    def impact_of(self, asset_id: str, version: Optional[int] = None) -> dict:
        """反向遍历引用图：资产发布新版本后会影响谁。

        判定在**版本粒度**上做不动点传播，再聚合到资产族 / 用例 / 套件：

        - 资产版本内容不可变，但含 ``follow`` 引用的版本，其**展开结果**会随
          被引用方发布新版而变化，视为「脏版本」；
        - 引用能传播变化，当且仅当它解析到的版本是脏的——follow 解析到
          对方最新版，lock 解析到锁定的具体版本；
        - 用例同理：直接引用，或经资产多层间接引用，沿路上每一跳都传播才算受影响；
        - ``outdated=False`` 的引用方就是「锁定旧版、本次升级不改变其结果」者。
        """
        family = self._families.get(asset_id)
        if family is None:
            raise AssetError("资产不存在")
        target_version = version or family["latest_version"]
        all_versions = self._versions.all()
        latest_no = {v["asset_id"]: 0 for v in all_versions}
        for v in all_versions:
            latest_no[v["asset_id"]] = max(latest_no[v["asset_id"]], v["version"])
        version_by_key = {(v["asset_id"], v["version"]): v for v in all_versions}

        def resolves_to(ref: dict) -> Optional[tuple[str, int]]:
            """引用按跟随策略实际解析到的 (资产族, 版本号)。"""
            fid = ref.get("asset_id")
            if ref.get("pin", "follow") == "lock":
                return fid, int(ref.get("version") or 0)
            return fid, latest_no.get(fid, 0)

        def root_propagates(ref: dict) -> bool:
            # 变化是否会沿这条引用传播：只有 follow 会自动拿到新版；
            # lock 永远解析到锁定的具体版本，新版不改变其展开结果。
            return ref.get("asset_id") == asset_id \
                and ref.get("pin", "follow") == "follow"

        def ref_is_stale(ref: dict) -> bool:
            # 锁定了比最新版更旧的版本（结果不变，但有新版本可升）
            return ref.get("pin") == "lock" and \
                int(ref.get("version") or 0) < latest_no.get(ref.get("asset_id"), 0)

        # -- 不动点：求脏版本集合 ----------------------------------------
        dirty: set[str] = set()  # version id

        def version_is_dirty(v: dict) -> bool:
            for ref in self._refs_in_steps(v.get("steps") or []):
                if root_propagates(ref):
                    return True
                fid, no = resolves_to(ref)
                if fid == asset_id:
                    continue
                target = version_by_key.get((fid, no))
                if target is not None and target["id"] in dirty:
                    return True
            return False

        for _ in range(len(all_versions) + 1):
            grew = False
            for v in all_versions:
                if v["id"] not in dirty and version_is_dirty(v):
                    dirty.add(v["id"])
                    grew = True
            if not grew:
                break

        # -- 聚合到资产族 ------------------------------------------------
        reverse_pins = self._reverse_pins(asset_id, dirty, version_by_key)
        affected_assets: dict[str, dict] = {}
        for v in all_versions:
            fid = v["asset_id"]
            if fid == asset_id or v["id"] not in dirty:
                continue
            fam = self._families.get(fid)
            item = affected_assets.setdefault(fid, {
                "asset_id": fid, "name": fam["name"] if fam else fid,
                "category": fam.get("category") if fam else "step",
                "latest_version": latest_no.get(fid),
                "outdated": False, "dirty_versions": set(),
                "pins": reverse_pins.get(fid, set()),
            })
            item["dirty_versions"].add(v["version"])
        # 族「对外受影响」：其最新版本是脏的（跟随它的引用方会拿到变化）
        for item in affected_assets.values():
            item["outdated"] = item["latest_version"] in item["dirty_versions"]

        # -- 传播到用例 --------------------------------------------------
        cases_store = self.registry.store("cases")
        affected_cases: dict[str, dict] = {}

        def case_is_outdated(case: dict) -> tuple[bool, bool, set]:
            pins: set = set()
            stale = False
            propagated = False
            for ref in self._refs_in_steps(case.get("steps") or []):
                pins.add(ref.get("pin", "follow"))
                if ref_is_stale(ref):
                    stale = True
                if root_propagates(ref):
                    propagated = True
                    continue
                fid, no = resolves_to(ref)
                if fid == asset_id:
                    continue
                target = version_by_key.get((fid, no))
                if target is not None and target["id"] in dirty:
                    propagated = True
            return propagated, stale, pins

        for case in cases_store.all():
            outdated, stale, pins = case_is_outdated(case)
            if not pins:
                continue
            affected_cases[case["id"]] = {
                "case_id": case["id"], "name": case.get("name"),
                "project_id": case.get("project_id"),
                "priority": case.get("priority"),
                "outdated": outdated, "stale": stale,
                "pins": sorted(pins),
            }

        # -- 套件 --------------------------------------------------------
        case_ids = {cid for cid, c in affected_cases.items()}
        affected_suites = []
        for suite in self.registry.store("suites").all():
            hit = [cid for cid in (suite.get("case_ids") or []) if cid in case_ids]
            if hit:
                affected_suites.append({
                    "suite_id": suite["id"], "name": suite.get("name"),
                    "project_id": suite.get("project_id"),
                    "case_count": len(hit),
                })

        return {
            "asset_id": asset_id,
            "version": target_version,
            "assets": [self._finalize_impact(v) for v in affected_assets.values()],
            "cases": list(affected_cases.values()),
            "suites": affected_suites,
            # outdated：跟随最新版、本次升级会立即改变展开结果
            "outdated_case_count": sum(1 for v in affected_cases.values() if v["outdated"]),
            # stale：锁定了旧版、结果不变但可升级；locked：全部锁定引用
            "stale_case_count": sum(1 for v in affected_cases.values() if v.get("stale")),
            "locked_case_count": sum(1 for v in affected_cases.values() if "follow" not in v["pins"]),
        }

    def _reverse_pins(self, root_id: str, dirty: set,
                      version_by_key: dict) -> dict:
        """各脏族「入边」上出现过的跟随策略集合（影响列表展示用）。

        ``dirty`` 里是版本记录 id；先用 (族, 版本号) 索引换回记录。
        """
        id_to_record = {v["id"]: v for v in version_by_key.values()}
        dirty_families = {id_to_record[vid]["asset_id"] for vid in dirty
                          if vid in id_to_record}
        targets = dirty_families | {root_id}
        pins: dict[str, set] = {}
        for v in version_by_key.values():
            for ref in self._refs_in_steps(v.get("steps") or []):
                if ref.get("asset_id") in targets and v["asset_id"] != root_id:
                    pins.setdefault(v["asset_id"], set()).add(
                        ref.get("pin", "follow"))
        return pins

    @staticmethod
    def _finalize_impact(item: dict) -> dict:
        out = dict(item)
        out["pins"] = sorted(item.get("pins", []))
        if "dirty_versions" in item:
            out["dirty_versions"] = sorted(item["dirty_versions"], reverse=True)
        if "via_versions" in item:
            out["via_versions"] = sorted(set(item["via_versions"]), reverse=True)
        return out

    # ------------------------------------------------------------------ 批量升级

    def batch_upgrade(self, asset_id: str, *, target_version: Optional[int] = None,
                      scope: str = "upgrade",
                      project_id: Optional[str] = None) -> dict:
        """批量处理引用方对某资产的版本跟随策略。

        ``scope`` 三种模式：

        - ``upgrade``（默认）：**锁定旧版**的引用升到目标版本（仍保持锁定）；
          跟随者本就拿最新，不动。即「批量升级到最新版」；
        - ``lock``：所有引用（含跟随者）锁定到目标版本，即「批量钉版」；
        - ``follow``：所有引用改为跟随最新版（同时记下目标版本号便于追溯）。

        用例步骤可直接改；但资产版本内容**不可变**——含待改引用的资产族
        通过「复制其最新版本 → 改写引用 → 发布新版本」完成升级，旧版本保留，
        这样锁定旧版的引用方行为依旧不变。
        """
        family = self._families.get(asset_id)
        if family is None:
            raise AssetError("资产不存在")
        target = int(target_version or family["latest_version"])
        if not self.get_version(asset_id, target):
            raise AssetError(f"目标版本 {target} 不存在")
        if scope not in ("upgrade", "lock", "follow"):
            raise AssetError("scope 只能是 upgrade / lock / follow")

        def rewrite(ref: dict) -> Optional[dict]:
            """返回改写后的引用；无需改动返回 None。"""
            pin = ref.get("pin", "follow")
            locked_old = pin == "lock" and int(ref.get("version") or 0) < target
            if scope == "upgrade":
                if locked_old:
                    return {**ref, "pin": "lock", "version": target}
                return None
            if scope == "lock":
                if pin == "lock" and int(ref.get("version") or 0) == target:
                    return None
                return {**ref, "pin": "lock", "version": target}
            # follow
            if pin == "follow":
                return None
            return {**ref, "pin": "follow", "version": target}

        def rewrite_steps(steps: list) -> tuple[list, bool]:
            touched = False
            out = []
            for s in steps or []:
                if isinstance(s, dict) and s.get("action") == USE_ASSET_ACTION \
                        and s.get("asset_id") == asset_id:
                    new_ref = rewrite({"asset_id": s.get("asset_id"),
                                       "version": s.get("version"),
                                       "pin": s.get("pin", "follow")})
                    if new_ref is not None:
                        s = {**s, **new_ref}
                        touched = True
                out.append(s)
            return out, touched

        # 先快照，避免下面发布新版本改变遍历集合
        snap_versions = [v for v in self._versions.all()
                         if v["asset_id"] != asset_id]
        snap_cases = self.registry.store("cases").all()

        # -- 资产：只对「最新版本含待改引用」的族发布新版本 ----------------
        republished: list[dict] = []
        seen_families: set[str] = set()
        for ver in snap_versions:
            fid = ver["asset_id"]
            if fid in seen_families or project_id and \
                    ver.get("project_id") != project_id:
                continue
            fam = self._families.get(fid)
            if fam is None:
                continue
            latest = self.get_version(fid, fam["latest_version"])
            if latest is None or latest["id"] != ver["id"]:
                continue  # 只处理最新版本；旧版本里的锁定引用保持原样
            new_steps, touched = rewrite_steps(latest.get("steps") or [])
            if touched:
                seen_families.add(fid)
                new_ver = self.publish_version(
                    fid, {"steps": new_steps,
                          "change_note": f"批量升级引用「{family['name']}」到 v{target}"})
                republished.append({"asset_id": fid, "name": fam["name"],
                                    "version": new_ver["version"]})

        # -- 用例：直接改写步骤 -------------------------------------------
        changed_cases: list[dict] = []
        cases_store = self.registry.store("cases")
        for case in snap_cases:
            if project_id and case.get("project_id") != project_id:
                continue
            new_steps, touched = rewrite_steps(case.get("steps") or [])
            if touched:
                cases_store.update(case["id"], {"steps": new_steps})
                changed_cases.append({"case_id": case["id"],
                                      "name": case.get("name")})

        return {"asset_id": asset_id, "target_version": target,
                "scope": scope,
                "republished_assets": republished,
                "changed_cases": changed_cases,
                "changed_asset_count": len(republished),
                "changed_case_count": len(changed_cases)}
