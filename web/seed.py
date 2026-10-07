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
    """生成演示项目，返回 ``{"project": ..., "env_id": ..., "suite_id": ...}``。"""
    from engine.assets import AssetLibrary

    proj = {
        "id": new_id("proj"),
        "name": "演示项目 · 测试与CI",
        "description": "内置示例用例、套件、环境、可复用资产与通知集成的演示项目。",
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
        "variables": {"BASE_URL": "http://dev.mock.local", "REGION": "dev",
                      "USERNAME": "admin", "PASSWORD": "dev-123456"},
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
        "variables": {"BASE_URL": "http://staging.mock.local", "REGION": "staging",
                      "USERNAME": "admin", "PASSWORD": "staging-secret"},
        "config": {"base_url": "http://staging.mock.local", "latency_ms": 45, "fail_rate": 0.15},
        "dependencies": [
            {"name": "requests", "constraint": ">=2.30"},
            {"name": "django", "constraint": ">=4.2"},
            {"name": "numpy", "constraint": ">=1.24"},
        ],
    })

    # ---------------------------------------------------------------- 可复用资产
    # 把几十条用例里反复抄写的东西抽出来：登录步骤流、状态码断言、变量模板。
    # 资产之间也互相引用（登录流引用「断言 200」片段），形成多层引用。
    lib = AssetLibrary(registry)

    # 1) 变量模板：同一资产在 dev / staging 取不同的环境变量值
    lib.create(pid, {
        "key": "vars_api_context",
        "name": "接口公共变量",
        "description": "用例公共的 region / 账号上下文，取值来自环境变量。",
        "category": "variables",
        "tags": ["common", "env"],
        "content": {
            "variables": {
                "region": "${REGION}",
                "base_url": "${BASE_URL}",
                "account": "${USERNAME}",
            },
        },
        "changelog": "初始版本",
    })

    # 2) 断言片段：响应状态码（参数化 expected）
    lib.create(pid, {
        "key": "assert_status_ok",
        "name": "断言响应成功",
        "description": "校验响应状态码等于期望值，默认 200。",
        "category": "assertion",
        "tags": ["common", "assert"],
        "content": {
            "params": {"code": 200},
            "steps": [
                {"action": "assert", "type": "status",
                 "actual": "${resp.status}", "expected": "${code}",
                 "name": "状态码等于 ${code}"},
            ],
        },
        "changelog": "初始版本",
    })

    # 3) 操作步骤流：通用登录。引用断言片段（多层引用），账号密码参数化，
    #    参数默认值来自环境变量——同一资产跨环境展开结果不同。
    lib.create(pid, {
        "key": "flow_login",
        "name": "通用登录步骤",
        "description": "准备账号 → 发起登录 → 断言成功。被多条用例引用。",
        "category": "step_flow",
        "tags": ["auth", "common"],
        "content": {
            "params": {
                "username": "${USERNAME}",
                "password": "${PASSWORD}",
                "path": "/api/login",
            },
            "steps": [
                {"action": "set", "key": "_login_user", "value": "${username}",
                 "name": "准备登录账号"},
                {"action": "request", "method": "POST", "url": "${path}",
                 "params": {"u": "${username}", "p": "${password}"},
                 "save_as": "resp", "name": "发起登录请求"},
                {"action": "use", "asset": "assert_status_ok",
                 "version": "latest", "params": {"code": 200},
                 "name": "校验登录响应"},
                {"action": "assert", "type": "contains",
                 "actual": "${resp.body}", "expected": "ok",
                 "name": "返回体包含 ok"},
            ],
        },
        "changelog": "初始版本",
    })

    # 给登录流发布 v2：登录前多一步区域校验，同时断言片段也发 v2（期望 201）。
    # 这样用例里可演示「锁定 v1」与「跟随 latest」两种引用的差异。
    lib.publish(pid, "assert_status_ok", {
        "changelog": "v2：断言更严格，默认期望 201（演示版本分化）",
        "content": {
            "params": {"code": 201},
            "steps": [
                {"action": "assert", "type": "status",
                 "actual": "${resp.status}", "expected": "${code}",
                 "name": "状态码等于 ${code}（v2）"},
            ],
        },
    })
    # 注意：flow_login 的 v1 里 assert_status_ok 用 latest，发布 v2 后，
    # 跟随 latest 的 flow_login 会自动看到新断言——影响沿引用链扩散。
    lib.publish(pid, "flow_login", {
        "changelog": "v2：登录前增加区域检查步骤",
        "content": {
            "params": {
                "username": "${USERNAME}",
                "password": "${PASSWORD}",
                "path": "/api/login",
            },
            "steps": [
                {"action": "assert", "type": "truthy", "actual": "${REGION}",
                 "expected": True, "name": "确认运行区域已注入"},
                {"action": "set", "key": "_login_user", "value": "${username}",
                 "name": "准备登录账号"},
                {"action": "request", "method": "POST", "url": "${path}",
                 "params": {"u": "${username}", "p": "${password}"},
                 "save_as": "resp", "name": "发起登录请求"},
                {"action": "use", "asset": "assert_status_ok",
                 "version": "latest", "params": {"code": 200},
                 "name": "校验登录响应"},
                {"action": "assert", "type": "contains",
                 "actual": "${resp.body}", "expected": "ok",
                 "name": "返回体包含 ok"},
            ],
        },
    })

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
    # 登录用例：一条 use 引用替代了原本重复的 4~5 个内联步骤，跟随最新版
    c2 = _case("登录接口（引用资产·跟随最新版）", "P0", ["smoke", "auth"], [
        {"action": "use", "asset": "flow_login", "version": "latest",
         "params": {}, "name": "通用登录（latest）"},
    ])
    # 锁定 v1：登录流升级到 v2、断言片段升级到 v2 都不会影响这条用例
    c_lock = _case("登录接口（引用资产·锁定 v1）", "P1", ["auth", "regression"], [
        {"action": "use", "asset": "flow_login", "version": "1",
         "params": {}, "name": "通用登录（锁定 v1）"},
    ])
    # 变量模板 + 资产引用：跨环境取值不同
    c_vars = _case("带环境变量模板的登录", "P1", ["auth", "env"], [
        {"action": "use", "asset": "vars_api_context", "version": "latest",
         "name": "注入公共变量"},
        {"action": "use", "asset": "flow_login", "version": "latest",
         "params": {"username": "${account}", "path": "/api/login?region=${region}"},
         "name": "用环境账号登录"},
        {"action": "assert", "type": "contains", "actual": "${resp.body.echo.url}",
         "expected": "region", "name": "URL 携带区域"},
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

    suite = {
        "id": new_id("suite"),
        "project_id": pid,
        "name": "冒烟测试套件",
        "description": "核心链路冒烟（含资产引用用例）",
        "group": "smoke",
        "env_id": env["id"],
        "case_ids": [c1, c2, c_lock, c_vars, c3, c4, c5, c6, c7, c8],
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
