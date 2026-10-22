"""AstrBot 适配层：把宿主的概念翻译成 PersonaTrace 的概念。

这一层刻意不 import astrbot —— 它只处理纯数据，能在没有 AstrBot 的环境里单测。
主机的身份、注入时机、渲染形态全部在这里收口，核心内核不感知宿主。
"""

from __future__ import annotations

import asyncio
import math
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


_PUNCT = "，。！？、；：「」『』（）()[]【】,.!?;: \t\n"
# 单个虚词按「字」去；时间/口头语按「词」去 —— 混在一起会把「在」「近」也删掉
_STOP_CHARS = "的了是我你他她它吗呢吧啊呀哦嘛"
_NOISE_WORDS = ("最近", "现在", "今天", "明天", "昨天", "这几天",
                "怎么样", "怎么", "什么", "这个", "那个", "是不是")


def _norm(text: str) -> str:
    out = (text or "").lower()
    for word in _NOISE_WORDS:
        out = out.replace(word, "")
    return "".join(ch for ch in out if ch not in _PUNCT and ch not in _STOP_CHARS).strip()


# 同义词归一：这是唯一能解决「零共同字」的轻量手段（练琴/吉他、复习/准备）。
# 只放高频、低歧义的口语替换；宁可少放，也别乱归并。
SYNONYMS = {
    "练琴": "吉他", "弹琴": "吉他", "学琴": "吉他",
    "备考": "准备", "复习": "准备", "刷题": "准备", "预习": "准备",
    "跳槽": "换工作", "离职": "换工作", "辞职": "换工作", "找工作": "换工作",
    "代码": "编程", "写码": "编程", "bug": "bug", "报错": "bug", "故障": "bug",
    "考试": "考", "面试": "面", "deadline": "截止",
}


def canonical(text: str) -> str:
    """按同义词表做替换（长词优先，避免「找工作」被「工作」截断）。"""
    out = text or ""
    for key in sorted(SYNONYMS, key=len, reverse=True):
        if key in out:
            out = out.replace(key, SYNONYMS[key])
    return out


def build_idf(labels) -> Dict[str, float]:
    """按当前库里的标签算字符权重：到处都有的字（在/的）权重低，稀有的字（吉/他）权重高。

    比硬编码停用字表更稳 —— 库变了权重自己跟着变，不用维护词表。
    """
    df: Dict[str, int] = {}
    for lb in labels:
        for ch in set(_norm(canonical(lb))):
            df[ch] = df.get(ch, 0) + 1
    n = max(1, len(labels))
    return {ch: math.log((n + 1) / (c + 1)) + 1.0 for ch, c in df.items()}


def text_similarity(a: str, b: str, idf: Optional[Dict[str, float]] = None) -> float:
    """字符 bigram 的 Jaccard + 字符重合度，纯标准库。

    不做语义；同义词靠 canonical() 归一，其余靠字面。有 idf 时用加权重合度，
    让常见字少贡献分数。
    """
    a, b = _norm(canonical(a)), _norm(canonical(b))
    if not a or not b:
        return 0.0
    if a in b or b in a:
        return 0.9
    ga = {a[i:i + 2] for i in range(len(a) - 1)} or {a}
    gb = {b[i:i + 2] for i in range(len(b) - 1)} or {b}
    bigram = len(ga & gb) / max(1, len(ga | gb))
    # 中文标签很短，bigram 容易全空；字符重合度更宽容，权重压低一点防误命中
    set_a, set_b = set(a), set(b)
    uni = len(set_a & set_b) / max(1, min(len(set_a), len(set_b)))
    score = max(bigram, uni * 0.8)
    if idf:
        hit = set_a & set_b
        if hit:
            total = sum(idf.get(ch, 1.0) for ch in set_a) or 1.0
            weighted = sum(idf.get(ch, 1.0) for ch in hit) / total
            score = max(score, weighted * 0.9)
    return score


MUST_KEEP = ("constraints", "boundaries")

# 方括号里的用途，决定模型怎么用它 —— 比只给一句话有用得多
CATEGORY_TAG = {
    "constraints": "约束", "boundaries": "边界", "goals": "目标", "projects": "在做",
    "likes": "偏好", "dislikes": "不喜欢", "identity": "自称", "explicit_facts": "自称",
    "state": "近况", "expertise": "擅长", "traits": "性格", "interests": "兴趣",
    "style_notes": "说话风格", "decision_style": "决策风格", "interpersonal": "人际",
}

# 方括号里的用途，决定模型怎么用它 —— 比只给一句话有用得多
CATEGORY_TAG = {
    "constraints": "约束", "boundaries": "边界", "goals": "目标", "projects": "在做",
    "likes": "偏好", "dislikes": "不喜欢", "identity": "自称", "explicit_facts": "自称",
    "state": "近况", "expertise": "擅长", "traits": "性格", "interests": "兴趣",
    "style_notes": "说话风格", "decision_style": "决策风格", "interpersonal": "人际",
}
"""硬约束和边界必须出现在注入里 —— 否则「说过不行的，不会再推荐」只是碰运气。"""


def pick_lines(rows: Iterable[Dict[str, Any]], max_items: int = 3,
               query: str = "") -> List[str]:
    rows = list(rows)
    # 硬约束留 1 格保底，剩下按「和这轮话题有多相关」排。
    # 不看话题的话，问什么都只会注入同样三条（实测过）。
    idf = build_idf([r.get("label", "") for r in rows])

    def rel(row) -> float:
        return text_similarity(query, row.get("label", ""), idf) if query else 0.0

    must = [r for r in rows if r.get("category") in MUST_KEEP]
    rest = [r for r in rows if r.get("category") not in MUST_KEEP]
    ordered = sorted(must, key=_rank)[: max(1, max_items - 1)] if must else []
    if len(ordered) < max_items:
        ranked = sorted(rest, key=lambda r: (rel(r),) + tuple(-x for x in _rank(r)))[::-1]
        if query:
            # 有明确相关的就只放相关的；没有就退回按紧迫度排（别硬凑无关条目）
            ranked = [r for r in ranked if rel(r) >= 0.12] or sorted(rest, key=_rank)
        ordered += ranked[: max_items - len(ordered)]

    items: List[str] = []
    for row in ordered:
        label = (row.get("label") or "").strip()
        if not label:
            continue
        tag = CATEGORY_TAG.get(row.get("category", ""), "")
        suffix = "，".join(x for x in (_due_text(row.get("due_in")), _staleness_text(row.get("staleness"))) if x)
        body = f"{label}（{suffix}）" if suffix else label
        items.append(f"[{tag}] {body}" if tag else body)
        if len(items) >= max_items:
            break
    return items


def render_memory(
    rows: Iterable[Dict[str, Any]],
    max_items: int = 3,
    max_chars: int = 240,
    header: str = "（这个人的记忆，方括号是用途：约束必须遵守，目标可主动关心，别逐条复述）",
    active: Optional[Dict[str, Any]] = None,
    directives: Optional[List[Dict[str, Any]]] = None,
    claims: Optional[List[Dict[str, Any]]] = None,
    query: str = "",
) -> str:
    """把记忆压成一段可以直接塞进对话的短文本；没有可用的就返回空串。

    active: active_hours() 的结果。样本够（≥3 天）才带一句「通常几点在线」，
    不够就不说 —— 猜错作息比不知道更烦人。
    """
    # 预算按类型分格：指令永远占 1 格（失效比少记一条偏好严重得多），
    # 其余给信念，还有空位才放转述。
    lines: List[str] = []
    for d in (directives or [])[:1]:
        lines.append(f"管理员交代：{d.get('label')}（照做，不用复述来源）")

    slot = max(0, max_items - len(lines))
    if slot:
        lines.extend(pick_lines(rows, max_items=slot, query=query))

    if len(lines) < max_items:
        ranked_claims = sorted(claims or [], key=lambda c: text_similarity(query, c.get("label", "")),
                               reverse=True)
        for c in ranked_claims[:1]:
            lines.append(f"{short_name(c.get('actor', ''))} 说{c.get('label')}（别人说的，别当事实）")
    if active and active.get("days", 0) >= 3:
        lines.append(f"通常 {active['start']}–{active['end']} 点在线")
    if not lines:
        return ""
    body = "；".join(lines)
    text = f"{header}{body}"
    while len(text) > max_chars and len(lines) > 1:
        lines.pop()                        # 宁可少一条，不要半条
        body = "；".join(lines)
        text = f"{header}{body}"
    return text[:max_chars]


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
