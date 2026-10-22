"""契约测试：用假 astrbot 模块跑插件骨架，不需要装 AstrBot。

验证四件事：
1. on_llm_request 只在有记忆时注入，且注入体不超过预算；
2. 两个人（不同 sender_id）互不可见；
3. /memory /del 的行为（含删除后清零）；
4. 注入顺序：快到期的排在前面。
"""

from __future__ import annotations

import asyncio
import sys
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))      # 让 `astrbot` 这个包名可用


def _install_fake_astrbot() -> None:
    def passthrough(*a, **kw):
        def deco(fn):
            return fn

        return deco

    astrbot = types.ModuleType("astrbot")
    api = types.ModuleType("astrbot.api")
    api_star = types.ModuleType("astrbot.api.star")
    api_event = types.ModuleType("astrbot.api.event")
    api_provider = types.ModuleType("astrbot.api.provider")
    core = types.ModuleType("astrbot.core")
    core_star_tools = types.ModuleType("astrbot.core.star.star_tools")

    class Star:
        pass

    class Context:
        pass

    class AstrMessageEvent:
        pass

    api_star.Star = Star
    api_star.Context = Context
    api_event.AstrMessageEvent = AstrMessageEvent

    flt = types.SimpleNamespace(
        command=passthrough,
        on_llm_request=passthrough,
        on_llm_response=passthrough,
        after_message_sent=passthrough,
        permission_type=passthrough,
        PermissionType=types.SimpleNamespace(ADMIN="admin", MEMBER="member"),
    )
    api_event.filter = flt

    class LLMResponse:
        def __init__(self, completion_text: str = "") -> None:
            self.completion_text = completion_text

    class ProviderRequest:
        def __init__(self) -> None:
            self.system_prompt = ""
            self.extra_user_content_parts = []

    api_provider.LLMResponse = LLMResponse
    api_provider.ProviderRequest = ProviderRequest
    api.logger = types.SimpleNamespace(
        info=lambda *a, **k: None,
        warning=lambda *a, **k: None,
        error=lambda *a, **k: None,
    )

    core_star_tools.StarTools = types.SimpleNamespace(get_data_dir=lambda name=None: HERE / "_data")

    api.star = api_star
    api.event = api_event
    api.provider = api_provider
    astrbot.api = api
    astrbot.core = core
    core.star = types.ModuleType("astrbot.core.star")
    core.star.star_tools = core_star_tools

    for name, mod in {
        "astrbot": astrbot,
        "astrbot.api": api,
        "astrbot.api.star": api_star,
        "astrbot.api.event": api_event,
        "astrbot.api.provider": api_provider,
        "astrbot.core": core,
        "astrbot.core.star": core.star,
        "astrbot.core.star.star_tools": core_star_tools,
    }.items():
        sys.modules[name] = mod


_install_fake_astrbot()

import astrbot_plugin_personatrace.main as plugin_main          # noqa: E402
from astrbot_plugin_personatrace import adapter                 # noqa: E402


class FakeEvent:
    def __init__(self, sender: str, text: str = "", platform: str = "aiocqhttp", group: str = "",
                 admin: bool = False, at: str = "") -> None:
        self._sender = sender
        self._platform = platform
        self._group = group
        self._admin = admin
        self._at = at
        self.message_str = text
        self.unified_msg_origin = f"{platform}:FriendMessage:{sender}"
        self._extra = {}

    def get_sender_id(self):
        return self._sender

    def get_platform_name(self):
        return self._platform

    def get_group_id(self):
        return self._group

    def is_admin(self):
        return self._admin

    def get_messages(self):
        return [types.SimpleNamespace(qq=self._at)] if self._at else []

    def set_extra(self, k, v):
        self._extra[k] = v

    def get_extra(self, k):
        return self._extra.get(k)

    def plain_result(self, text):
        return text


def req():
    return plugin_main.ProviderRequest()


def make_plugin():
    """契约测试不落盘：注入内存后端，持久化由 test_persistence.py 单独覆盖。"""
    from personatrace.memory_v2 import MemoryBackend

    ctx = types.SimpleNamespace(get_using_provider=lambda umo=None: None)
    plugin = plugin_main.Main(context=ctx, config={"inject_max_items": 3})
    plugin._backend = MemoryBackend()
    return plugin


def test_cold_start_injects_nothing():
    p = make_plugin()
    e = FakeEvent("1001", "你好")
    r = req()
    asyncio.run(p.inject_memory(e, r))
    assert r.extra_user_content_parts == [], "空库时不该注入任何东西"


def test_inject_and_budget():
    p = make_plugin()
    e = FakeEvent("1001")
    key = adapter.identity_key("aiocqhttp", "1001")
    mem = p._mem(key)
    mem.observe("goals", "交付驻车集 V1.0", track="work", confidence=0.8, due_at_ts=__import__("time").time() + 2 * 86400)
    mem.observe("likes", "偏好深色主题", track="work")
    r = req()
    asyncio.run(p.inject_memory(e, r))
    assert len(r.extra_user_content_parts) == 1
    part = r.extra_user_content_parts[0]
    text = part["text"] if isinstance(part, dict) else part.text
    assert "记忆" in text and "驻车集" in text
    assert len(text) <= 240, f"注入超预算: {len(text)}"
    assert text.index("驻车集") < text.index("深色主题"), "快到期的应排前面"


def test_two_users_isolated():
    p = make_plugin()
    a, b = FakeEvent("1001"), FakeEvent("2002")
    p._mem(adapter.identity_key("aiocqhttp", "1001")).observe("goals", "A 的私事", track="work")
    ra, rb = req(), req()
    asyncio.run(p.inject_memory(a, ra))
    asyncio.run(p.inject_memory(b, rb))
    assert ra.extra_user_content_parts, "A 应看到自己的记忆"
    assert rb.extra_user_content_parts == [], "B 不该看到 A 的记忆"


def test_memory_and_del_commands():
    p = make_plugin()
    key = adapter.identity_key("aiocqhttp", "1001")
    p._mem(key).observe("goals", "在准备四级", track="work")

    async def run(coro):
        return [x async for x in coro]

    out = asyncio.run(run(p.show_memory(FakeEvent("1001", f"/memory {key}"))))
    assert out and "四级" in out[0] and "1 条" in out[0]

    out = asyncio.run(run(p.delete_memory(FakeEvent("1001", f"/del {key}"))))
    assert "已删除" in out[0] and "1 条" in out[0]

    out = asyncio.run(run(p.show_memory(FakeEvent("1001", f"/memory {key}"))))
    assert "0 条" in out[0]


def test_bad_key_rejected():
    p = make_plugin()

    async def run(coro):
        return [x async for x in coro]

    out = asyncio.run(run(p.delete_memory(FakeEvent("1001", "/del ../etc"))))
    assert "不合法" in out[0]


def test_inject_only_once_per_turn():
    """agent 回路会多次请求模型；同一轮只该注入一次，否则记忆会重复堆进上下文。"""
    p = make_plugin()
    e = FakeEvent("1001")
    p._mem(adapter.identity_key("aiocqhttp", "1001")).observe("goals", "一次就够", track="work")
    r1, r2 = req(), req()
    asyncio.run(p.inject_memory(e, r1))
    asyncio.run(p.inject_memory(e, r2))
    assert len(r1.extra_user_content_parts) == 1
    assert r2.extra_user_content_parts == [], "第二轮不该重复注入"


def test_per_group_scope():
    p = make_plugin()
    p.cfg["scope"] = "per_group"
    a = FakeEvent("1001", group="g1")
    b = FakeEvent("2002", group="g1")
    c = FakeEvent("1001", group="g2")
    assert p._identity(a) == p._identity(b), "同群应按群共享"
    assert p._identity(a) != p._identity(c)
    p.cfg["scope"] = "per_user"
    assert p._identity(a) != p._identity(b), "per_user 下应按人分开"


def test_extract_bridge_writes_into_memory_v2():
    """写入腿：抽取结果 → MemoryV2（不经过 persona_manager 那套存储）。"""
    from personatrace.memory_v2 import MemoryBackend, MemoryV2

    class FakeExtractor:
        def extract_from_conversation(self, **kwargs):
            return {
                "work_updates": {
                    "goals": [{"label": "在准备四级", "confidence": 0.8, "reasoning": "用户自己说的"}],
                    "likes": [{"label": "偏好深色主题"}],
                },
                "persona_updates": {"identity": [{"label": "计算机专业学生"}]},
            }

    mem = MemoryV2(backend=MemoryBackend(), user_id="aiocqhttp:1001")
    bridge = adapter.ExtractBridge(mem, FakeExtractor())
    n = bridge.ingest("我在准备四级，专业课也忙", "加油")
    rows = mem.state()
    assert n == 3 and len(rows) == 3, (n, rows)
    tracks = {r["category"]: r.get("track") for r in rows}
    assert tracks.get("goals") == "work" and tracks.get("identity") == "persona"


def test_host_llm_bridges_sync_call_to_async_provider():
    """抽取跑在线程池里，但 provider 是 async —— 这条桥必须通。"""
    import threading

    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()

    class FakeProvider:
        async def text_chat(self, prompt=None, contexts=None, system_prompt=None, model=None):
            assert "hi" in (prompt or "")
            return types.SimpleNamespace(completion_text='{"ok": 1}')

    try:
        llm = adapter.HostLLM(FakeProvider(), loop, timeout=5)
        assert llm.chat_json([{"role": "user", "content": "hi"}]) == {"ok": 1}
    finally:
        loop.call_soon_threadsafe(loop.stop)


def test_remember_writes_into_memory_after_reply():
    """写入腿全链路：after_message_sent → provider → 抽取 → MemoryV2。"""
    p = make_plugin()
    payload = '{"work_updates": {"goals": [{"label": "在准备四级", "confidence": 0.8}]}, "persona_updates": {}}'

    class FakeProvider:
        async def text_chat(self, prompt=None, contexts=None, system_prompt=None, model=None):
            return types.SimpleNamespace(completion_text=payload)

    async def fake_get_provider(umo=None):
        return FakeProvider()

    p.context.get_using_provider_async = fake_get_provider
    p.cfg["window_cold_sec"] = 0          # 关掉节流，确保这次一定触发
    e = FakeEvent("1001", "我在准备四级，专业课也忙")
    e.set_extra("_pt_reply", "加油")

    async def run():
        await p.remember(e)
        mem = p._mem(p._identity(e))
        for _ in range(40):
            await asyncio.sleep(0.05)
            if mem.state():
                return mem.state()
        return mem.state()

    rows = asyncio.run(run())
    assert rows and rows[0]["label"] == "在准备四级", rows


def test_constraints_always_survive_ranking():
    """硬约束/边界必须入选，否则「说过不行的，不会再推荐」只是碰运气。"""
    rows = [
        {"label": "在准备四级", "category": "goals", "due_in": 2, "effective_confidence": 0.9, "staleness": 0.1},
        {"label": "不接受远程办公", "category": "constraints", "due_in": None, "effective_confidence": 0.1, "staleness": 0.9},
        {"label": "偏好深色主题", "category": "likes", "due_in": None, "effective_confidence": 0.8, "staleness": 0.1},
        {"label": "别在早上找他", "category": "boundaries", "due_in": None, "effective_confidence": 0.1, "staleness": 0.5},
    ]
    lines = adapter.pick_lines(rows, 3)
    assert any("不接受远程办公" in x for x in lines), lines
    assert any("别在早上找他" in x for x in lines), lines
    assert len(lines) == 3


def test_consolidate_update_replaces_old_belief():
    """「我换到字节了」→ 旧结论失效、新结论生效，不并存。"""
    from personatrace.memory_v2 import MemoryBackend, MemoryV2

    class FakeLLM:
        def __init__(self, payload):
            self.payload = payload

        def chat_json(self, messages, max_tokens=8192, model=None):
            return self.payload

    mem = MemoryV2(backend=MemoryBackend(), user_id="qq:21")
    mem.observe("projects", "在腾讯做后端", track="work", confidence=0.8)
    short = mem.state()[0]["id"][:8]
    llm = FakeLLM({"update": [{"id": short, "label": "在字节做后端", "reason": "用户说换了"}]})

    res = adapter.Consolidator(mem, llm).run("我换到字节了", "恭喜")
    assert res == {"updated": 1, "deleted": 0}, res

    active = [r["label"] for r in mem.state()]
    assert active == ["在字节做后端"], active
    assert len(mem.state(include_invalidated=True)) == 2, "旧结论应保留为已失效"


def test_consolidate_rejects_made_up_id():
    from personatrace.memory_v2 import MemoryBackend, MemoryV2

    class FakeLLM:
        def chat_json(self, messages, max_tokens=8192, model=None):
            return {"update": [{"id": "deadbeef", "label": "编的"}],
                    "delete": [{"id": "deadbeef"}]}

    mem = MemoryV2(backend=MemoryBackend(), user_id="qq:22")
    mem.observe("goals", "在准备四级", track="work")
    res = adapter.Consolidator(mem, FakeLLM()).run("随便说点什么", "嗯")
    assert res == {"updated": 0, "deleted": 0}, res
    assert [r["label"] for r in mem.state()] == ["在准备四级"]


def test_consolidate_delete_and_cap():
    from personatrace.memory_v2 import MemoryBackend, MemoryV2

    mem = MemoryV2(backend=MemoryBackend(), user_id="qq:23")
    for i in range(8):
        mem.observe("goals", f"目标 {i}", track="work", confidence=0.8)
    shorts = [r["id"][:8] for r in mem.state()]

    class FakeLLM:
        def chat_json(self, messages, max_tokens=8192, model=None):
            return {"delete": [{"id": s} for s in shorts]}      # 8 条，超上限

    res = adapter.Consolidator(mem, FakeLLM()).run("这些都不做了", "好")
    assert res["deleted"] == 5, res                        # max_ops 生效
    assert len(mem.state()) == 3, len(mem.state())


def test_active_hours_only_when_enough_days():
    """活跃时段要有 ≥3 天样本才说；不够就说不知道。"""
    from datetime import datetime, timedelta

    from personatrace.memory_v2 import MemoryBackend, MemoryV2

    mem = MemoryV2(backend=MemoryBackend(), user_id="qq:11")
    noon = datetime.now().replace(hour=23, minute=10, second=0, microsecond=0)
    for d in range(4):
        mem.record_behavior("chat", "group", ts=(noon - timedelta(days=d)).timestamp())
    hint = mem.active_hours()
    assert hint and hint["days"] == 4 and hint["start"] <= 23 < hint["end"] + 24, hint

    text = adapter.render_memory(mem.state() or [{"label": "x"}], active=hint)
    assert "点在线" in text

    fresh = MemoryV2(backend=MemoryBackend(), user_id="qq:12")
    fresh.record_behavior("chat", "group")
    assert fresh.active_hours() is None
    assert "点在线" not in adapter.render_memory([{"label": "x"}], active=fresh.active_hours())


def test_parse_due_only_when_certain():
    """认得出明确日期，认不出就返回 None —— 宁可漏提醒，不要提醒错的事。"""
    from datetime import datetime

    now = datetime(2026, 10, 21, 5, 55)          # 周三
    assert adapter.parse_due("这周末模拟考", now) is not None
    assert datetime.fromtimestamp(adapter.parse_due("明天交作业", now)).day == 22
    assert datetime.fromtimestamp(adapter.parse_due("下周三面试", now)).day == 28
    assert adapter.parse_due("我最近在学吉他", now) is None
    assert adapter.parse_due("", now) is None


def test_bridge_attaches_due_date_for_goals():
    """写入时把「这周末」解析成截止时间，注入排序才有东西可用。"""
    from personatrace.memory_v2 import MemoryBackend, MemoryV2

    class FakeExtractor:
        def extract_from_conversation(self, **kwargs):
            return {"work_updates": {"goals": [{"label": "这周末模拟考", "confidence": 0.8}]},
                    "persona_updates": {}}

    mem = MemoryV2(backend=MemoryBackend(), user_id="qq:9")
    adapter.ExtractBridge(mem, FakeExtractor()).ingest("我这周末要模拟考", "加油")
    row = mem.state()[0]
    assert row["due_in"] is not None, "goals 应带截止时间"
    assert 0 < row["due_in"] <= 8, row["due_in"]


def test_admin_directive_goes_to_the_target_not_the_admin():
    """「以后碰到他就骂他」必须落在**被说的人**的库里，管理员自己看不到就对了。"""
    p = make_plugin()
    admin = FakeEvent("100", "以后碰到他就骂他", group="g1", admin=True, at="200")
    p._record_directive(admin, admin.message_str)

    target = p._mem(adapter.identity_key("aiocqhttp", "200"))
    assert [d["label"] for d in target.directives(group_key="g1")] == ["以后碰到他就骂他"]
    assert p._mem(adapter.identity_key("aiocqhttp", "100")).directives() == []


def test_non_admin_directive_ignored():
    p = make_plugin()
    fake = FakeEvent("100", "以后碰到他就骂他", group="g1", admin=False, at="200")
    p._record_directive(fake, fake.message_str)
    assert p._mem(adapter.identity_key("aiocqhttp", "200")).directives() == []


def test_directive_is_injected_for_target():
    p = make_plugin()
    admin = FakeEvent("100", "以后碰到他就骂他", group="g1", admin=True, at="200")
    p._record_directive(admin, admin.message_str)

    e = FakeEvent("200", group="g1")
    r = req()
    asyncio.run(p.inject_memory(e, r))
    text = r.extra_user_content_parts[0]
    text = text["text"] if isinstance(text, dict) else text.text
    assert "管理员交代" in text and "骂他" in text, text


def test_inject_follows_the_topic():
    """问什么就想起来什么 —— 之前不传 query，三次问不同问题拿到同样三条。"""
    rows = [{"label": "在学吉他", "category": "state", "effective_confidence": 0.6, "staleness": 0.1},
            {"label": "在准备四级", "category": "goals", "effective_confidence": 0.6, "staleness": 0.1},
            {"label": "最近在改支付 bug", "category": "state", "effective_confidence": 0.6, "staleness": 0.1},
            {"label": "不接受远程办公", "category": "constraints", "effective_confidence": 0.3, "staleness": 0.8}]

    # 条目现在带 [用途] 前缀，用子串判断
    assert any("在学吉他" in x for x in adapter.pick_lines(rows, 3, query="吉他最近练得怎么样"))
    assert any("在准备四级" in x for x in adapter.pick_lines(rows, 3, query="四级报名了吗"))
    assert any("最近在改支付 bug" in x for x in adapter.pick_lines(rows, 3, query="支付那个 bug 怎么样了"))


def test_synonyms_bridge_zero_overlap():
    """「练琴」和「在学吉他」零共同字，只能靠同义词表。"""
    assert adapter.text_similarity("练琴", "在学吉他") > 0.5
    assert adapter.text_similarity("复习得怎么样", "在准备四级") > 0.2
    assert adapter.text_similarity("跳槽的事", "想换工作") > 0.4
    # 无关的话不能被同义词表带偏
    assert adapter.text_similarity("今天天气不错", "在准备四级") < 0.2


def test_idf_downweights_common_chars():
    """到处都有的字（在/的）不该和稀有字（吉/他）一样重要。"""
    labels = ["在准备四级", "在做驻车集", "在学吉他", "最近在改支付bug"]
    idf = adapter.build_idf(labels)
    assert idf["在"] < idf["吉"], "到处都有的字权重应更低"
    assert idf["在"] < idf["驻"]
    assert abs(idf["吉"] - idf["驻"]) < 1e-9, "同样稀有的字权重应相同"


def test_inject_without_query_still_works():
    """没传 query（旧调用方）不能炸，退回按紧迫度排。"""
    rows = [{"label": "在学吉他", "category": "state", "effective_confidence": 0.6, "staleness": 0.1}]
    picked = adapter.pick_lines(rows, 3)
    assert len(picked) == 1 and "在学吉他" in picked[0]


def test_directive_takes_exactly_one_slot():
    """3 格预算里指令占 1 格，不会把信念挤没，也不会自己膨胀。"""
    rows = [{"label": f"偏好 {i}", "category": "likes", "effective_confidence": 0.9 - i * 0.1,
             "staleness": 0.1, "due_in": None} for i in range(4)]
    text = adapter.render_memory(
        rows, max_items=3,
        directives=[{"label": "碰到他就骂他"}, {"label": "第二条指令"}],
        claims=[{"actor": "aiocqhttp:9", "label": "很忙"}],
    )
    assert text.count("管理员交代") == 1, text
    assert text.count("；") <= 3, text
    assert "偏好 0" in text, text


def test_memory_command_reports_mentions():
    p = make_plugin()
    key = adapter.identity_key("aiocqhttp", "1001")
    mem = p._mem(key)
    mem.observe("goals", "在准备四级", track="work")
    mem.observe_mention("aiocqhttp:2002", "claim", "state", "别人说他很忙")

    async def run(coro):
        return [x async for x in coro]

    out = asyncio.run(run(p.show_memory(FakeEvent("1001", f"/memory {key}"))))
    assert "另有 1 条转述" in out[0], out[0]


def test_hearsay_and_self_claim_are_marked():
    """别人说的标成「别人说的」，自称标成「自称」，都不能当事实。"""
    p = make_plugin()
    mem = p._mem(adapter.identity_key("aiocqhttp", "200"))
    mem.observe("identity", "是管理员的爸爸", track="persona")
    mem.observe_mention("aiocqhttp:300", "claim", "state", "最近很忙")

    e = FakeEvent("200")
    r = req()
    asyncio.run(p.inject_memory(e, r))
    text = r.extra_user_content_parts[0]
    text = text["text"] if isinstance(text, dict) else text.text
    assert "自称" in text, text
    assert "别当事实" in text, text


def test_loads_like_astrbot_does(tmp_path, monkeypatch):
    """按 AstrBot 的方式加载：__import__("data.plugins.<name>.main", fromlist=["main"])。

    这条验证的是相对导入 `from . import adapter` 在宿主的加载方式下能否成立
    —— 用 sys.path 直接 import 是测不出来的。
    """
    import shutil

    plugin_dir = HERE      # 仓库根就是插件目录
    target = tmp_path / "data" / "plugins" / "astrbot_plugin_personatrace"
    target.parent.mkdir(parents=True)
    shutil.copytree(plugin_dir, target, ignore=shutil.ignore_patterns("_data", "__pycache__"))

    monkeypatch.syspath_prepend(str(tmp_path))
    for name in [m for m in sys.modules if m.startswith("data.plugins")]:
        del sys.modules[name]

    mod = __import__("data.plugins.astrbot_plugin_personatrace.main", fromlist=["main"])
    assert hasattr(mod, "Main")
    assert hasattr(mod, "adapter") or mod.adapter is not None


if __name__ == "__main__":
    for fn in (
        test_cold_start_injects_nothing,
        test_inject_and_budget,
        test_two_users_isolated,
        test_memory_and_del_commands,
        test_bad_key_rejected,
        test_extract_bridge_writes_into_memory_v2,
        test_host_llm_bridges_sync_call_to_async_provider,
        test_inject_only_once_per_turn,
        test_per_group_scope,
        test_constraints_always_survive_ranking,
        test_admin_directive_goes_to_the_target_not_the_admin,
        test_non_admin_directive_ignored,
        test_directive_is_injected_for_target,
        test_inject_follows_the_topic,
        test_synonyms_bridge_zero_overlap,
        test_idf_downweights_common_chars,
        test_inject_without_query_still_works,
        test_inject_follows_the_topic,
        test_synonyms_bridge_zero_overlap,
        test_idf_downweights_common_chars,
        test_inject_without_query_still_works,
        test_directive_takes_exactly_one_slot,
        test_memory_command_reports_mentions,
        test_inject_follows_the_topic,
        test_synonyms_bridge_zero_overlap,
        test_idf_downweights_common_chars,
        test_inject_without_query_still_works,
        test_inject_follows_the_topic,
        test_synonyms_bridge_zero_overlap,
        test_idf_downweights_common_chars,
        test_inject_without_query_still_works,
        test_directive_takes_exactly_one_slot,
        test_memory_command_reports_mentions,
        test_hearsay_and_self_claim_are_marked,
        test_consolidate_update_replaces_old_belief,
        test_consolidate_rejects_made_up_id,
        test_consolidate_delete_and_cap,
        test_admin_directive_goes_to_the_target_not_the_admin,
        test_non_admin_directive_ignored,
        test_directive_is_injected_for_target,
        test_inject_follows_the_topic,
        test_synonyms_bridge_zero_overlap,
        test_idf_downweights_common_chars,
        test_inject_without_query_still_works,
        test_inject_follows_the_topic,
        test_synonyms_bridge_zero_overlap,
        test_idf_downweights_common_chars,
        test_inject_without_query_still_works,
        test_directive_takes_exactly_one_slot,
        test_memory_command_reports_mentions,
        test_inject_follows_the_topic,
        test_synonyms_bridge_zero_overlap,
        test_idf_downweights_common_chars,
        test_inject_without_query_still_works,
        test_inject_follows_the_topic,
        test_synonyms_bridge_zero_overlap,
        test_idf_downweights_common_chars,
        test_inject_without_query_still_works,
        test_directive_takes_exactly_one_slot,
        test_memory_command_reports_mentions,
        test_hearsay_and_self_claim_are_marked,
        test_consolidate_update_replaces_old_belief,
        test_consolidate_rejects_made_up_id,
        test_consolidate_delete_and_cap,
        test_active_hours_only_when_enough_days,
        test_parse_due_only_when_certain,
        test_bridge_attaches_due_date_for_goals,
    ):
        fn()
        print(f"[PASS] {fn.__name__}")
