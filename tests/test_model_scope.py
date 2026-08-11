"""调用级模型作用域测试（DeepTutor request-scoped 模型选择移植）。

覆盖:
- model_scope 块内覆盖生效，块外恢复
- 无 profile 的 role 用轻量覆盖配置
- 作用域只对声明 role 生效
- contextvars 隔离（async 并发场景）
"""

from __future__ import annotations

import asyncio

from src.swarm.model_config import (
    get_model_profile,
    model_scope,
    upsert_model_profile,
)


def test_no_scope_returns_normal(db):
    upsert_model_profile(db, "capture", "deepseek", "v4-flash", is_default=True)
    prof = get_model_profile(db, "capture")
    assert prof is not None
    assert prof["model"] == "v4-flash"


def test_scope_overrides_and_restores(db):
    upsert_model_profile(db, "capture", "deepseek", "v4-flash", is_default=True)
    with model_scope(db, "capture", model="deepseek-v4-pro"):
        prof = get_model_profile(db, "capture")
        assert prof is not None
        assert prof["model"] == "deepseek-v4-pro"
        # 覆盖保留 base 的其他字段
        assert prof["provider"] == "deepseek"
    # 块外恢复原值
    prof = get_model_profile(db, "capture")
    assert prof is not None
    assert prof["model"] == "v4-flash"


def test_scope_returns_lightweight_for_unknown_role(db):
    """role 无专属 profile 时，scope 以声明 role 为准（base 可来自 custom 兜底）。"""
    with model_scope(db, "ghost-role", model="deepseek-v4-flash"):
        prof = get_model_profile(db, "ghost-role")
        assert prof is not None
        assert prof["model"] == "deepseek-v4-flash"
        assert prof["role"] == "ghost-role"
    # 块外无 scope → 回落到 custom 兜底 profile（迁移种子数据）
    prof = get_model_profile(db, "ghost-role")
    assert prof is None or prof["model"] != "deepseek-v4-flash"


def test_scope_only_affects_declared_role(db):
    upsert_model_profile(db, "capture", "deepseek", "v4-flash", is_default=True)
    with model_scope(db, "capture", model="deepseek-v4-pro"):
        # 其他 role 解析不受影响（analyst 有迁移种子的默认 profile）
        prof = get_model_profile(db, "analyst")
        assert prof is not None
        assert prof["model"] == "reasoning"
        assert get_model_profile(db, "capture")["model"] == "deepseek-v4-pro"


def test_scope_ignores_nonmatching_role_lookup(db):
    upsert_model_profile(db, "capture", "deepseek", "v4-flash", is_default=True)
    upsert_model_profile(db, "analyst", "deepseek", "v4-pro", is_default=True)
    with model_scope(db, "capture", model="deepseek-v4-pro"):
        prof = get_model_profile(db, "analyst")
        assert prof is not None
        assert prof["model"] == "v4-pro"


def test_scope_ignores_none_overrides(db):
    upsert_model_profile(db, "capture", "deepseek", "v4-flash", is_default=True)
    with model_scope(db, "capture", model=None, temperature=0.2):
        prof = get_model_profile(db, "capture")
        assert prof is not None
        assert prof["model"] == "v4-flash"  # None override 不生效
        assert prof["temperature"] == 0.2


def test_async_scope_isolation(db):
    """contextvars 隔离：两个并发 task 各自读到自己 scope 的覆盖值。"""
    upsert_model_profile(db, "capture", "deepseek", "v4-flash", is_default=True)

    async def worker(role, model):
        async def inner():
            with model_scope(db, role, model=model):
                await asyncio.sleep(0.01)
                prof = get_model_profile(db, role)
                return prof["model"] if prof else None

        return await inner()

    async def main():
        results = await asyncio.gather(
            worker("capture", "model-A"),
            worker("capture", "model-B"),
        )
        return results

    results = asyncio.run(main())
    assert sorted(results) == ["model-A", "model-B"]
