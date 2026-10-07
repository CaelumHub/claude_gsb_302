"""演示 / 初始数据生成。

应用启动时，若数据目录里还没有任何项目，会自动调用 :func:`seed_demo_data`
生成一份演示数据（项目 + 用例 + 套件 + 环境 + 计划 + 集成），让各个页面
一打开就有内容可点、可测。HTTP 接口 ``POST /api/seed/demo`` 也复用这里，
供前端「生成演示项目」按钮调用。
"""

from __future__ import annotations

import time

from engine import new_id


def seed_demo_data(registry, env_mgr, notify_mgr) -> dict:
    from engine.assets import AssetLibrary, USE_ASSET_ACTION
    assets = AssetLibrary(registry)
    """生成演示项目，返回 ``{"project": ..., "env_id": ..., "suite_id": ...}``。"""
    proj = {
        "id": new_id("proj"),
        "name": "演示项目 · 测试与CI",
        "description": "内置示例用例、套件、环境与通知集成的演示项目。",
        "repo_url": "https://example.com/demo",
        "auto_create_defects": True,
        "created_at": time.time(),
    }
    registry.store("projects").insert(proj)
    pid = proj["id"]

    env = env_mgr.create(pid, {
        "name": "dev 开发环境",
        "python_version": "3.11",
        "base_image": "python:3.11-slim",
        "variables": {"BASE_URL": "http://dev.mock.local", "REGION": "dev"},
        "config": {"base_url": "http://dev.mock.local", "latency_ms": 15, "fail_rate": 0.0},
        "dependencies": [
            {"name": "requests", "constraint": ">=2.28"},
            {"name": "pytest", "constraint": ">=7.0"},
            {"name": "flask", "constraint": ">=3.0"},
        ],
    })
    env2 = env_mgr.create(pid, {
        "name": "staging 预发环境",
        "python_version": "3.12",
        "base_image": "python:3.12-slim",
        "variables": {"BASE_URL": "http://staging.mock.local", "REGION": "staging"},
        "config": {"base_url": "http://staging.mock.local", "latency_ms": 45, "fail_rate": 0.15},
        "dependencies": [
            {"name": "requests", "constraint": ">=2.30"},
            {"name": "django", "constraint": ">=4.2"},
            {"name": "numpy", "constraint": ">=1.24"},
        ],
    })

    # ------------------------------------------------------------------
    # 可复用资产：变量模板 → 底层操作 → 组合登录（多层引用）
    # ------------------------------------------------------------------
    # 1) 变量模板：同一资产在 dev / staging 下 ${BASE_URL} 取值不同
    a_vars = assets.create_family(pid, {
        "name": "接口环境变量", "category": "variables",
        "description": "接口用例共用的地址 / 区域变量，执行时由环境变量覆盖。",
        "tags": ["env", "公共"],
        "steps": [
            {"action": "set", "key": "BASE_URL", "value": "${BASE_URL}", "name": "基础地址（取自环境）"},
            {"action": "set", "key": "REGION", "value": "${REGION}", "name": "区域（取自环境）"},
        ],
        "change_note": "初始版本",
    })

    # 2) 底层操作：登录请求（v1 走 /api/login，v2 改为 /api/v2/login，
    #    用来演示「发布新版 → 影响沿引用链扩散 → 锁定/跟随差异」）
    a_login_req = assets.create_family(pid, {
        "name": "登录请求", "category": "step",
        "description": "发起登录并把响应保存为 resp。",
        "tags": ["auth", "登录"],
        "steps": [
            {"action": "set", "key": "user", "value": "admin", "name": "准备用户名"},
            {"action": "request", "method": "POST", "url": "/api/login",
             "body": {"user": "${user}"}, "save_as": "resp", "name": "请求登录 v1"},
        ],
    })
    assets.publish_version(a_login_req["id"], {
        "steps": [
            {"action": "set", "key": "user", "value": "admin", "name": "准备用户名"},
            {"action": "request", "method": "POST", "url": "/api/v2/login",
             "body": {"user": "${user}", "remember": True}, "save_as": "resp",
             "name": "请求登录 v2"},
        ],
        "change_note": "登录接口升级到 v2，增加 remember 参数",
    })

    # 3) 断言片段
    a_login_assert = assets.create_family(pid, {
        "name": "登录成功断言", "category": "assertion",
        "description": "校验登录返回 200 且响应体含 ok。",
        "tags": ["auth", "断言"],
        "steps": [
            {"action": "assert", "type": "status", "actual": "${resp.status}",
             "expected": 200, "name": "状态码 200"},
            {"action": "assert", "type": "contains", "actual": "${resp.body}",
             "expected": "ok", "name": "返回体含 ok"},
        ],
    })

    # 4) 组合资产：标准登录 = 变量模板 + 登录请求 + 断言（资产引用资产）
    #    变量与登录请求都跟随最新版，底层一发新版就会沿链扩散到此资产
    a_login = assets.create_family(pid, {
        "name": "标准登录流程", "category": "step",
        "description": "变量注入 + 登录 + 成功断言，被多条用例复用。",
        "tags": ["auth", "公共", "冒烟"],
        "steps": [
            {"action": "use_asset", "asset_id": a_vars["id"],
             "version": 1, "pin": "follow", "name": "引入环境变量"},
            {"action": "use_asset", "asset_id": a_login_req["id"],
             "version": 1, "pin": "follow", "name": "登录请求（跟随最新）"},
            {"action": "use_asset", "asset_id": a_login_assert["id"],
             "version": 1, "pin": "follow", "name": "登录成功断言"},
        ],
    })

    a_health = assets.create_family(pid, {
        "name": "健康检查片段", "category": "step",
        "description": "请求健康检查并断言 200。",
        "tags": ["smoke", "公共"],
        "steps": [
            {"action": "request", "method": "GET", "url": "/api/health",
             "name": "请求健康检查"},
            {"action": "assert", "type": "status", "actual": "${resp.status}",
             "expected": 200, "name": "状态码 200"},
        ],
    })

    def _ref(family, pin="follow", name=None):
        return {"action": "use_asset", "asset_id": family["id"],
                "version": family["latest_version"], "pin": pin,
                "name": name or family["name"]}

    def _case(name, priority, tags, steps):
        return registry.store("cases").insert({
            "id": new_id("case"),
            "project_id": pid,
            "name": name,
            "description": "演示用例",
            "priority": priority,
            "tags": tags,
            "timeout": 60,
            "enabled": True,
            "steps": steps,
            "created_at": time.time(),
        })

    c1 = _case("健康检查接口", "P0", ["smoke", "api"], [
        {"action": "request", "method": "GET", "url": "/api/health", "name": "请求健康检查"},
        {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200, "name": "状态码 200"},
        {"action": "assert", "type": "truthy", "actual": "${resp.body.ok}", "expected": True, "name": "返回 ok"},
    ])
    c2 = _case("登录接口", "P0", ["smoke", "auth"], [
        {"action": "set", "key": "user", "value": "admin", "name": "准备用户名"},
        {"action": "request", "method": "POST", "url": "/api/login", "name": "请求登录"},
        {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200, "name": "登录成功"},
        {"action": "assert", "type": "contains", "actual": "${resp.body}", "expected": "ok", "name": "返回体含 ok"},
    ])
    c3 = _case("用户列表查询", "P1", ["api", "users"], [
        {"action": "request", "method": "GET", "url": "/api/users", "name": "查询用户列表"},
        {"action": "script", "expr": "len([1,2,3])", "save_as": "count", "name": "计算数量"},
        {"action": "assert", "type": "gte", "actual": "${count}", "expected": 3, "name": "数量 >= 3"},
    ])
    c4 = _case("创建项目", "P1", ["api", "projects"], [
        {"action": "request", "method": "POST", "url": "/api/projects", "name": "创建项目"},
        {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200, "name": "状态码 200"},
    ])
    c5 = _case("慢接口（性能）", "P2", ["perf"], [
        {"action": "request", "method": "GET", "url": "/api/slow", "name": "请求慢接口"},
        {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200, "name": "状态码 200"},
    ])
    c6 = _case("失败注入接口", "P2", ["chaos"], [
        {"action": "request", "method": "GET", "url": "/api/error", "name": "请求失败接口"},
        {"action": "assert", "type": "status", "actual": "${resp.status}", "expected": 200, "name": "期望 200"},
    ])
    c7 = _case("字符串断言", "P2", ["unit"], [
        {"action": "script", "expr": "2 + 3 * 4", "save_as": "result", "name": "算术"},
        {"action": "assert", "type": "equals", "actual": "${result}", "expected": 14, "name": "结果等于 14"},
        {"action": "assert", "type": "between", "actual": "${result}", "expected": [10, 20], "name": "结果在 10~20"},
    ])
    c8 = _case("正则断言", "P3", ["unit"], [
        {"action": "set", "key": "text", "value": "release-2.31.0", "name": "设置文本"},
        {"action": "assert", "type": "regex", "actual": "${text}", "expected": r"^\d+\.\d+", "name": "匹配版本号"},
    ])
    # 资产复用用例：
    #  c9  引用组合资产（跟随）：底层「登录请求」发新版会沿多层链扩散过来
    #  c10 直接锁定「登录请求 v1」：结果停留在旧版，影响分析里标记为可升级
    c9 = _case("登录-资产复用（跟随最新版）", "P0", ["smoke", "auth", "资产"], [
        _ref(a_login, pin="follow", name="标准登录（跟随）"),
        {"action": "assert", "type": "truthy", "actual": "${REGION}", "name": "区域变量已注入"},
    ])
    c10 = _case("登录-资产复用（锁定登录v1）", "P0", ["smoke", "auth", "资产"], [
        _ref(a_login_req, pin="lock", name="登录请求（锁定 v1）"),
        {"action": "assert", "type": "status", "actual": "${resp.status}",
         "expected": 200, "name": "状态码 200"},
    ])
    c11 = _case("健康检查-资产复用", "P1", ["smoke", "资产"], [
        _ref(a_health),
        {"action": "assert", "type": "truthy", "actual": "${resp.body.ok}", "name": "返回 ok"},
    ])

    suite = {
        "id": new_id("suite"),
        "project_id": pid,
        "name": "冒烟测试套件",
        "description": "核心链路冒烟",
        "group": "smoke",
        "env_id": env["id"],
        "case_ids": [c1, c2, c3, c4, c5, c6, c7, c8, c9, c10, c11],
        "created_at": time.time(),
    }
    registry.store("suites").insert(suite)

    registry.store("schedules").insert({
        "id": new_id("sch"),
        "project_id": pid,
        "name": "每 10 分钟跑一次冒烟",
        "cron": "*/10 * * * *",
        "suite_id": suite["id"],
        "env_id": env["id"],
        "enabled": False,
        "last_fired_minute": None,
        "created_at": time.time(),
    })

    notify_mgr.create(pid, {
        "type": "webhook",
        "name": "CI Webhook",
        "config": {"url": "https://example.com/hooks/ci"},
        "events": ["build.finished", "build.failed"],
    })
    notify_mgr.create(pid, {
        "type": "email",
        "name": "团队邮件",
        "config": {"address": "qa@example.com"},
        "events": ["build.failed"],
    })

    return {"project": proj, "env_id": env["id"], "suite_id": suite["id"]}
