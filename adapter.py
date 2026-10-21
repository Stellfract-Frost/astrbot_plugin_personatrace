"""AstrBot 适配层：把宿主的概念翻译成 PersonaTrace 的概念。

这一层刻意不 import astrbot —— 它只处理纯数据，能在没有 AstrBot 的环境里单测。
主机的身份、注入时机、渲染形态全部在这里收口，核心内核不感知宿主。
"""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional

from personatrace.llm_client import extract_json

_KEY_RE = re.compile(r"^[A-Za-z0-9_.:@\-]{1,64}$")


# ---------------------------------------------------------------- 身份


def identity_key(platform: str, sender_id: str) -> str:
    """把（平台, 发送者）压成一个稳定的记忆键。

    例如 aiocqhttp + 12345 -> "aiocqhttp:12345"。
    用平台前缀而不是裸 QQ 号：Telegram 用户没有 QQ 号，裸号会全塌成一个用户。
    """
    platform = (platform or "unknown").strip().lower()
    sender_id = str(sender_id or "").strip()
    if not sender_id:
        raise ValueError("sender_id is empty; refusing to build a shared memory key")
    key = f"{platform}:{sender_id}"
    if not _KEY_RE.match(key):
        # 平台 id 里出现奇怪字符时兜底：只保留安全字符，避免污染命名空间
        key = re.sub(r"[^A-Za-z0-9_.:@\-]", "_", key)[:64]
    return key


def key_from_event_parts(platform: str, sender_id: str, group_id: str = "", scope: str = "per_user") -> str:
    """scope=per_user（默认，一人一份）或 per_group（整群共享一份关系记忆）。"""
    if scope == "per_group" and group_id:
        return identity_key(platform, f"group:{group_id}")
    return identity_key(platform, sender_id)


def normalize_key(raw: str) -> str:
    """管理员输入 /memory <key> 时的校验（防止空 key 或脏 key 打到别人库上）。"""
    key = (raw or "").strip()
    if not key:
        raise ValueError("key 为空")
    if not _KEY_RE.match(key):
        raise ValueError(f"key 不合法：{key!r}（只允许字母数字和 _.:@-）")
    return key


# ---------------------------------------------------------------- 整理（改 / 删）

CONSOLIDATE_PROMPT = """你在维护一份关于某个人的记忆清单。判断这轮对话是否**推翻了**其中某条。

现有记忆（id | 类别 | 内容 | 上次支持距今）：
{candidates}

这轮对话：
用户：{user}
助手：{reply}

只输出 JSON，不要解释：
{{"update": [{{"id": "8位id", "label": "新的说法", "reason": "为什么被推翻"}}],
  "delete": [{{"id": "8位id", "reason": "为什么不再成立"}}]}}

规则：
- id 必须来自上面的清单，**编造的会被丢弃**
- update 只用于「同一个对象、新的说法」（换了工作/改了目标/改口了）
- delete 只用于「做完了 / 取消了 / 明确说不再这样」
- 只是补充新信息、没有推翻旧结论时，两个数组都留空
- 最多 5 条 update、5 条 delete；没把握就不要改"""


class Consolidator:
    """让模型判断：这轮对话有没有推翻已有结论。

    先给短 id（8 位）再回填校验 —— 模型复制 32 位 uuid 极易出错，
    短 id 映射表能同时解决「编造」和「抄错」两种失败。
    """

    def __init__(self, mem: Any, llm: Any, max_candidates: int = 30, max_ops: int = 5) -> None:
        self.mem = mem
        self.llm = llm
        self.max_candidates = max_candidates
        self.max_ops = max_ops

    def _candidates(self, user_msg: str):
        rows = self.mem.state()[: self.max_candidates]
        lines = []
        mapping = {}
        for row in rows:
            short = row["id"][:8]
            mapping[short] = row
            stale = row.get("staleness")
            lines.append(
                f"- {short} | {row.get('category')} | {row.get('label')}"
                + (f" | {stale:.1f}" if isinstance(stale, (int, float)) else "")
            )
        return "\n".join(lines), mapping

    def run(self, user_msg: str, reply: str) -> Dict[str, int]:
        candidates, mapping = self._candidates(user_msg)
        if not mapping:
            return {"updated": 0, "deleted": 0}

        prompt = CONSOLIDATE_PROMPT.format(
            candidates=candidates, user=user_msg[:1500], reply=(reply or "")[:600]
        )
        data = self.llm.chat_json([{"role": "user", "content": prompt}]) or {}

        updated = deleted = 0
        for op in (data.get("update") or [])[: self.max_ops]:
            if not isinstance(op, dict):
                continue
            row = mapping.get(str(op.get("id", "")).strip())
            label = str(op.get("label") or "").strip()
            if not row or len(label) < 2 or label == row.get("label"):
                continue                      # 编造 id / 空标签 / 没变化：一律丢弃
            self.mem.observe(
                row["category"],
                label,
                track=row.get("track", "work"),
                confidence=float(row.get("confidence") or 0.6),
                reasoning=f"consolidate: {op.get('reason', '')}"[:200],
                evidence_text=user_msg[:1000],
            )
            self.mem.invalidate(row["id"])
            updated += 1

        for op in (data.get("delete") or [])[: self.max_ops]:
            if not isinstance(op, dict):
                continue
            row = mapping.get(str(op.get("id", "")).strip())
            if not row:
                continue
            self.mem.invalidate(row["id"], status="superseded")
            deleted += 1

        return {"updated": updated, "deleted": deleted}


# ---------------------------------------------------------------- 第三方


RULE_VERBS = ("骂", "别理", "不要理", "无视", "拉黑", "踢", "警告", "怼", "禁言", "别管", "敷衍")


def looks_like_rule(text: str) -> bool:
    """粗略判断是不是「以后怎么对待某人」。只是第一道筛，真正的授权在宿主侧。"""
    t = text or ""
    return any(v in t for v in RULE_VERBS)


def at_keys(qq_ids: Iterable[str], platform: str = "aiocqhttp") -> List[str]:
    return [identity_key(platform, str(q)) for q in qq_ids if str(q or "").strip()]


def short_name(key: str) -> str:
    """aiocqhttp:2002 -> 2002；给人看的短标识。"""
    return (key or "").split(":")[-1] or (key or "某人")


# ---------------------------------------------------------------- 渲染


def _due_text(due_in: Optional[float]) -> str:
    if due_in is None:
        return ""
    if due_in < 0:
        return "已过期"
    if due_in < 1:
        return "今天到期"
    return f"还剩 {int(due_in)} 天"


def _staleness_text(staleness: Optional[float]) -> str:
    if staleness is None or staleness < 1:
        return ""
    return f"{int(staleness * 30)} 天没再提"


def _rank(row: Dict[str, Any]) -> tuple:
    """先按紧迫度，再按有效置信度。故意不按字母序，避免每次都注入同几条。"""
    due = row.get("due_in")
    urgency = 0 if due is not None and due <= 7 else (1 if row.get("needs_review") else 2)
    return (urgency, -(row.get("effective_confidence") or 0.0))


MUST_KEEP = ("constraints", "boundaries")
"""硬约束和边界必须出现在注入里 —— 否则「说过不行的，不会再推荐」只是碰运气。"""


def pick_lines(rows: Iterable[Dict[str, Any]], max_items: int = 3) -> List[str]:
    rows = list(rows)
    must = [r for r in rows if r.get("category") in MUST_KEEP]
    rest = [r for r in rows if r.get("category") not in MUST_KEEP]
    ordered = sorted(must, key=_rank)[: max(1, max_items - 1)]
    if len(ordered) < max_items:
        ordered += [r for r in sorted(rest, key=_rank) if r not in ordered][: max_items - len(ordered)]

    items: List[str] = []
    for row in ordered:
        label = (row.get("label") or "").strip()
        if not label:
            continue
        if row.get("category") in ("identity", "explicit_facts"):
            label = f"自称{label}"          # 未经核实的身份宣称，不当作事实
        suffix = "，".join(x for x in (_due_text(row.get("due_in")), _staleness_text(row.get("staleness"))) if x)
        items.append(f"{label}（{suffix}）" if suffix else label)
        if len(items) >= max_items:
            break
    return items


def render_memory(
    rows: Iterable[Dict[str, Any]],
    max_items: int = 3,
    max_chars: int = 240,
    header: str = "（关于这个人的记忆，供参考，别逐条复述）",
    active: Optional[Dict[str, Any]] = None,
    directives: Optional[List[Dict[str, Any]]] = None,
    claims: Optional[List[Dict[str, Any]]] = None,
) -> str:
    """把记忆压成一段可以直接塞进对话的短文本；没有可用的就返回空串。

    active: active_hours() 的结果。样本够（≥3 天）才带一句「通常几点在线」，
    不够就不说 —— 猜错作息比不知道更烦人。
    """
    lines = pick_lines(rows, max_items=max_items)
    # 指令优先：管理员交代的事比偏好重要
    for d in (directives or [])[:2]:
        lines.insert(0, f"管理员交代：{d.get('label')}（照做，不用复述来源）")
    for c in (claims or [])[:2]:
        lines.append(f"{short_name(c.get('actor', ''))} 说{c.get('label')}（别人说的，别当事实）")
    if active and active.get("days", 0) >= 3:
        lines.append(f"通常 {active['start']}–{active['end']} 点在线")
    if not lines:
        return ""
    body = "；".join(lines)
    text = f"{header}{body}"
    if len(text) > max_chars:
        text = text[: max_chars - 1] + "…"
    return text


# ---------------------------------------------------------------- 截止时间

_DAY_WORDS = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "日": 7, "天": 7,
              "1": 1, "2": 2, "3": 3, "4": 4, "5": 5, "6": 6, "7": 7}


def _at(day: datetime, hour: int = 9) -> float:
    return day.replace(hour=hour, minute=0, second=0, microsecond=0).timestamp()


def parse_due(text: str, now: Optional[datetime] = None) -> Optional[float]:
    """从一句话里找出「什么时候到期」。认不出来就返回 None，绝不猜。

    只做确定性的词面匹配：今天/明天/后天、周末、下周X、N 天后、X月X号、月底。
    宁可漏（不提醒）也不要错（提醒错的事）。
    """
    now = now or datetime.now()
    t = (text or "").strip()
    if not t:
        return None

    m = re.search(r"(\d+)\s*天(?:之后|以后|后|内|之内)", t)
    if m:
        return _at(now + timedelta(days=int(m.group(1))))

    for word, delta in (("今天", 0), ("今晚", 0), ("明天", 1), ("明晚", 1), ("后天", 2), ("大后天", 3)):
        if word in t:
            return _at(now + timedelta(days=delta), 20 if "晚" in word else 9)

    m = re.search(r"(下|这|本)?周(?:末|星期|礼拜)?([一二三四五六日天1-7])", t)
    if m:
        weekday = _DAY_WORDS.get(m.group(2))
        if weekday:
            ahead = (weekday - 1 - now.weekday()) % 7
            if m.group(1) == "下":
                ahead += 7
            elif m.group(1) in ("这", "本") and ahead == 0 and "周" in t:
                ahead = 7 if now.hour >= 12 else 0
            target = now + timedelta(days=ahead)
            return _at(target)

    if "周末" in t:
        ahead = (5 - now.weekday()) % 7
        if ahead == 0 and now.hour >= 18:
            ahead = 7
        return _at(now + timedelta(days=ahead))

    m = re.search(r"(\d{1,2})\s*月\s*(\d{1,2})\s*[日号]", t)
    if m:
        month, day = int(m.group(1)), int(m.group(2))
        year = now.year + (1 if month < now.month else 0)
        try:
            return _at(now.replace(year=year, month=month, day=day))
        except ValueError:
            return None

    if "月底" in t:
        nxt = (now.replace(day=1) + timedelta(days=32)).replace(day=1)
        return _at(nxt - timedelta(days=1))

    return None


# ---------------------------------------------------------------- 抽取（复用宿主模型）


class HostLLM:
    """把 AstrBot 的 provider 伪装成 PersonaTrace 的 LLMClient（鸭子类型）。

    extractor 是同步的、跑在线程池里；provider.text_chat 是 async。
    所以这里用 run_coroutine_threadsafe 把调用丢回主事件循环，线程安全的桥。
    """

    def __init__(self, provider: Any, loop: asyncio.AbstractEventLoop, timeout: float = 90.0) -> None:
        self.provider = provider
        self.loop = loop
        self.timeout = timeout

    async def _call(self, prompt: str, model: Optional[str]) -> Any:
        return await self.provider.text_chat(prompt=prompt, contexts=[], system_prompt="", model=model)

    def chat(self, messages: List[Dict[str, str]], max_tokens: int = 4096, model: Optional[str] = None) -> str:
        prompt = "\n".join(m.get("content", "") for m in messages if m.get("role") == "user")
        fut = asyncio.run_coroutine_threadsafe(self._call(prompt, model), self.loop)
        resp = fut.result(timeout=self.timeout)
        return getattr(resp, "completion_text", "") or ""

    def chat_json(
        self,
        messages: List[Dict[str, str]],
        max_tokens: int = 8192,
        model: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        return extract_json(self.chat(messages, max_tokens=max_tokens, model=model))


class ExtractBridge:
    """一轮对话结束后，把抽取结果写进 MemoryV2。

    注意：这里刻意不走 persona_manager —— 那条链路写的是另一套存储（ProfileData + chromadb），
    和 MemoryV2 从来没连通。适配层只认 MemoryV2，插件因此不需要 chromadb / sentence-transformers。
    """

    def __init__(self, mem: Any, extractor: Any, max_per_turn: int = 8) -> None:
        self.mem = mem
        self.extractor = extractor
        self.max_per_turn = max_per_turn

    def ingest(self, user_msg: str, ai_reply: str, history: str = "") -> int:
        result = self.extractor.extract_from_conversation(
            user_msg=user_msg,
            ai_reply=ai_reply,
            history_context=history,
            now=datetime.now(),
            vector_store=None,      # 不带向量库：靠 uuid5 主键 + hits 强化做去重
            user_id=self.mem.user_id,
        )
        if not result:
            return 0
        written = 0
        for track, block in (
            ("work", result.get("work_updates") or {}),
            ("persona", result.get("persona_updates") or {}),
        ):
            if not isinstance(block, dict):
                continue
            for category, items in block.items():
                for item in items or []:
                    if written >= self.max_per_turn:
                        return written
                    if isinstance(item, dict):
                        label = str(item.get("label") or "").strip()
                        confidence = float(item.get("confidence") or 0.5)
                        reasoning = str(item.get("reasoning") or "")
                    else:
                        label, confidence, reasoning = str(item).strip(), 0.5, ""
                    if len(label) < 2:
                        continue
                    due = None
                    if track == "work" and category in ("goals", "projects"):
                        # 先看标签本身有没有日期，没有再退回用户原话（只对目标/项目做）
                        due = parse_due(label) or parse_due(user_msg or "")
                    self.mem.observe(
                        category,
                        label,
                        track=track,
                        confidence=max(0.0, min(1.0, confidence)),
                        reasoning=reasoning,
                        evidence_text=(user_msg or "")[:1000],
                        due_at_ts=due,
                    )
                    written += 1
        return written
