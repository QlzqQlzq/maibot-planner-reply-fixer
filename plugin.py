"""planner 决策兜底插件（小模型决策辅助）。

背景：小参数量模型在 planner 阶段经常把「要调用的工具」写成一段文字说明，例如：

    决策：调用reply工具，让希罗发出一条温和的简短回应。
    调用reply工具：
    - msg_id: "-123456"
    - expression_intent:
      - tone: 温和、柔和
      - focus: 对方的「摸摸头」请求

它并没有真的发起 tool call，宿主于是判定「无调用」，角色什么都不说。
本插件把这段说明还原成一次真实工具调用；台词仍由宿主 replayer 生成。

两种输入都能兜：
1. 结构化：`调用X工具：\n- key: value`，按工具自己的 schema 过滤参数。
2. 散文式：「可以简短接一句」「调用reply工具，语气轻松自然」——
   从**上一轮请求里缓存下来的上下文**取出目标消息的 msg_id，把这段文字压成
   reply_reference，拼成一次 reply 调用。

安全约定（都是踩过的坑）：
1. 只在 `tool_names` 白名单里选工具；散文模式优先 reply，绝不盲取白名单第一项。
2. 工具定义只来自 `maisaka.planner.before_request` 的 `tool_definitions`；拿不到就不做。
3. 上下文只用于取 msg_id，且跳过自己发的消息与被引用的旧消息。
4. 否定语义（不需要回复／不应该回应）优先于回复意图。
5. `msg_id`、`reply_reference`、`logical_turn_id` 缺一个就放弃，不硬发。
6. 回写时保留 Reasoning / Provider Activity，只替换 AssistantMessageItem。
"""

import re
import time
import uuid
from copy import deepcopy
from datetime import datetime
from typing import Any

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

HOOK_BEFORE = "maisaka.planner.before_request"
HOOK_AFTER = "maisaka.planner.after_response"
_TURN_TTL = 300.0
_TURN_MAX = 64

# 「调用 xxx 工具」的说明句
_SPEC_HEAD = re.compile(r"调用\s*([A-Za-z_][A-Za-z0-9_]*)\s*工具|call\s+([A-Za-z_][A-Za-z0-9_]*)\s+tool", re.I)
# 「- key: value」列表项（中英文冒号都认）
_SPEC_ITEM = re.compile(r"^(?P<indent>[ \t]*)[-*]\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*[:：]\s*(?P<val>.*)$")
# msg_id：只认 `msg_id=123` / `msg_id: 123`
_MSG_ID_STRICT = re.compile(r"msg_id\s*[=:：]\s*[\"']?(-?\d+)[\"']?", re.I)
# 上下文里的 <message msg_id="..." quote="..." is_self_message="true">
_CTX_TAG = re.compile(r"<message\s+([^>]*)>", re.I)
_CTX_ATTR = re.compile(r'([A-Za-z_]+)\s*=\s*"([^"]*)"')

# 明确表示「要走 reply 工具」
_EXPLICIT_REPLY = re.compile(r"(?:通过|使用|用|调用)\s*reply|reply\s*工具", re.I)
# 否定式：不需要回复／不应该回应／不适合接一句
_NEG_INTENT = re.compile(
    r"(?:不|没|别|勿|无)\s*(?:需要|应该|适合|建议|宜|必|用|要)?\s*(?:再)?\s*"
    r"(?:回复|回应|接一句|说一句|搭话|插一句)",
    re.I,
)
# 回复意图（正向表述）
_REPLY_INTENT = re.compile(
    r"(?:通过|使用|用|调用)\s*reply|reply\s*工具|"
    r"(?:可以|应该|需要|适合|自然|直接|建议|宜)\s*(?:简短|自然|轻松)?\s*(?:地)?\s*"
    r"(?:回复|回应|接一句|说一句|搭话|插一句)|"
    r"接一句|说一句|搭个话",
    re.I,
)
# 第三人称主体（只认多字词，避免「其他/其它」误伤口语里的「他/她」）
_THIRD_PERSON = re.compile(r"(?:对方|他们|她们|别人|群友|群成员|其他人|其它人)")
# 强静默：明确表示不参与
_SILENCE_STRONG = re.compile(
    r"保持静默|保持沉默|保持安静|不说话|暂不发言|不要说话|不必说话|"
    r"不用搭理|不必理会|无需参与|无需介入|"
    r"stay\s+silent|do\s*not\s+call\s+any\s+tool|don'?t\s+call\s+any\s+tool|no\s+tool\s+call",
    re.I,
)
# 弱否定：可以被「明确要走 reply」抵消
_SILENCE_WEAK = re.compile(
    r"不进行工具调用|不进行任何工具调用|不调用|无需调用|不要调用|不做任何操作|不回复",
    re.I,
)


class PluginSectionConfig(PluginConfigBase):
    """插件元信息段（宿主校验 config_version 用）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="宿主侧插件开关（该字段由宿主维护）")
    config_version: str = Field(default="0.1.14", description="配置版本")


class FixerConfig(PluginConfigBase):
    """兜底行为配置。"""

    __ui_label__ = "决策兜底"
    __ui_icon__ = "wrench"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="兜底开关")
    tool_names: list[str] = Field(
        default_factory=lambda: ["reply"],
        description="允许兜底的工具白名单。模型散文里点到别的工具一律不认",
    )
    prose_mode: bool = Field(
        default=True,
        description="模型只写「可以简短接一句」这类散文时，用上下文里的 msg_id 硬拼一次调用",
    )
    prose_tool_name: str = Field(
        default="reply",
        description="散文模式使用的工具名（必须在 tool_names 白名单里）",
    )
    enabled_spec_parsing: bool = Field(
        default=True,
        description="是否解析「调用xxx工具：- key: value」这类说明；关掉则整个插件不工作",
    )
    synth_reply_reference: bool = Field(
        default=True,
        description="缺少 reply_reference 时，用 focus / reply_act / tone / scene 拼一句",
    )
    dry_run: bool = Field(default=False, description="只写日志、不改写输出（观察用）")
    min_text_chars: int = Field(default=10, ge=0, le=2000, description="模型输出短于该长度时忽略")
    max_reference_chars: int = Field(default=200, ge=40, le=400, description="散文转 reply_reference 的字数上限")
    silence_keywords: list[str] = Field(
        default_factory=list,
        description="额外的静默关键词（内置静默判断始终生效，这里只做补充）",
    )


class PlannerReplyFixerConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    fixer: FixerConfig = Field(default_factory=FixerConfig)


# ---------------------------------------------------------------- 文本工具


def _squash(text: str) -> str:
    return re.sub(r"\s+", " ", text or "").strip()


def _item_text(item: Any) -> str:
    if not isinstance(item, dict):
        return ""
    parts = item.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks = []
    for part in parts:
        if isinstance(part, dict) and part.get("type") == "text":
            value = part.get("text")
            if isinstance(value, str) and value.strip():
                chunks.append(value)
    return "\n".join(chunks)


def _has_tool_call(items: list[Any]) -> bool:
    for item in items:
        if not isinstance(item, dict):
            continue
        if item.get("item_type") == "FunctionCallItem":
            return True
        tool_call = item.get("tool_call")
        if isinstance(tool_call, dict) and tool_call.get("func_name"):
            return True
    return False


def _reply_intent(text: str) -> bool:
    """模型是不是在表达「我（角色）要说话」。

    - 出现否定式（不需要回复/不适合接一句）且没有明确的「调用 reply」，视为不说话
    - 只描述别人（对方/他们/其他人）该回，视为不说话
    """
    if _NEG_INTENT.search(text) and not _EXPLICIT_REPLY.search(text):
        return False
    for mt in _REPLY_INTENT.finditer(text):
        window = text[max(0, mt.start() - 10) : mt.start()]
        if _THIRD_PERSON.search(window):
            continue
        return True
    return False


def _build_reference(text: str, limit: int) -> str:
    """把模型那段散文压成 reply_reference（给 replayer 的方向说明）。

    - 去掉尾部括注（「（本轮…）」这类）
    - 丢掉含「无需调用/不必回复」的句子，避免和「要回复」自相矛盾
    - 只在句末或逗号处截断，不留半句话
    """
    body = text
    for marker in ("**决策：**", "**决策:**", "决策：", "决策:", "**分析：**", "分析：", "分析:", "决定：", "方案："):
        if marker in body:
            body = body.split(marker, 1)[1]
            break

    body = re.sub(r"msg_id\s*[=:：]\s*[\"']?-?\d+[\"']?", "", body)
    body = re.sub(r"(?m)^\s*[-*]\s*[A-Za-z_]+\s*[:：].*$", "", body)
    body = re.sub(r"调用\s*(?:reply|回复工具)\s*工具?", "", body)
    body = _squash(body)
    body = re.sub(r"^工具[\s，,。:：]*", "", body)

    # 去尾部括注（闭合的 + 被截断未闭合的）
    for _ in range(2):
        trimmed = re.sub(r"[（(][^（）()]*[）)]\s*[。；;]?\s*$", "", body).strip()
        if trimmed == body:
            break
        body = trimmed
    body = re.sub(r"[（(][^（）()]*$", "", body).strip()

    # 按分句过滤：只丢掉「无需调用工具」这类否定分句，保留方向描述
    neg = re.compile(r"(?:无需|不需要|不必|不用|不进行|不做|暂不)[^。！？；，,]{0,12}(?:工具|调用|回复|发言|回应)")
    sentences = re.split(r"(?<=[。！？；;!?])", body)
    kept_sentences = []
    for sent in sentences:
        if not sent.strip():
            continue
        clauses = re.split(r"(?<=[，,、])", sent)
        kept_clauses = [c for c in clauses if not neg.search(c)]
        merged = "".join(kept_clauses).strip()
        if merged:
            kept_sentences.append(merged)
    body = "".join(kept_sentences).strip() or body

    # 长度控制：先按句末，再退化到逗号
    if len(body) > limit:
        head = body[:limit]
        cut = max(head.rfind("。"), head.rfind("！"), head.rfind("？"), head.rfind("；"), head.rfind(";"))
        if cut >= max(20, limit // 2):
            body = head[: cut + 1]
        else:
            cut2 = max(head.rfind("，"), head.rfind(","), head.rfind("、"))
            body = head[:cut2] if cut2 >= max(12, limit // 3) else head

    body = body.strip(" 　-—:：。，,")
    if len(body) < 8:
        body = _squash(text)[:limit]
    return body



def _find_msg_id_in_context(items: list[Any]) -> str:
    """从上一轮请求的上下文里取最新一条（非自己发的、非被引用的）消息 id。"""
    latest = ""
    for item in items:
        text = _item_text(item)
        if not text:
            continue
        clean = text.replace('\\"', '"').replace("\\\\", "")
        for tag in _CTX_TAG.findall(clean):
            attrs = dict(_CTX_ATTR.findall(tag))
            msg_id = str(attrs.get("msg_id") or "").strip()
            if not msg_id:
                continue
            if str(attrs.get("is_self_message") or "").lower() == "true":
                continue
            if attrs.get("quote") and attrs.get("quote") == msg_id:
                continue
            latest = msg_id
    return latest


def _normalize_definitions(raw: Any) -> list[dict[str, Any]]:
    """把几种工具定义形态统一成 [{name, parameters}]。"""
    out: list[dict[str, Any]] = []
    seen = set()

    def _walk(node: Any, depth: int = 0) -> None:
        if depth > 6:
            return
        if isinstance(node, dict):
            tools = node.get("tools")
            if isinstance(tools, list):
                for sub in tools:
                    _walk(sub, depth + 1)
                return
            fn = node.get("function") if isinstance(node.get("function"), dict) else None
            definition = node.get("definition") if isinstance(node.get("definition"), dict) else None
            source = fn or definition or node

            name = source.get("name") or node.get("name")
            params = source.get("parameters")
            if not isinstance(params, dict):
                params = source.get("parameters_schema")
            if not isinstance(params, dict):
                params = node.get("parameters_schema")
            if isinstance(name, str) and name.strip():
                name = name.strip()
                if name not in seen:
                    seen.add(name)
                    out.append({"name": name, "parameters": params if isinstance(params, dict) else {}})
            return
        if isinstance(node, list):
            for sub in node:
                _walk(sub, depth + 1)

    _walk(raw)
    return out


def _parse_spec(text: str) -> tuple[str, dict[str, Any]]:
    """解析「- key: value」结构，返回 (点名的工具, 参数字典)。"""
    tool = ""
    m = _SPEC_HEAD.search(text)
    if m:
        tool = (m.group(1) or m.group(2) or "").strip()

    values: dict[str, Any] = {}
    parent = None
    parent_indent = -1

    for line in text.splitlines():
        item = _SPEC_ITEM.match(line)
        if not item:
            if parent is not None and line.strip() and not line.strip().startswith(("-", "*")):
                target = values.get(parent)
                if isinstance(target, str) and target:
                    values[parent] = target + " " + line.strip()
            continue

        indent = len(item.group("indent"))
        key = item.group("key")
        val = item.group("val").strip()

        if not val:
            parent = key
            parent_indent = indent
            values.setdefault(key, {})
            continue

        if parent is not None and indent > parent_indent and isinstance(values.get(parent), dict):
            values[parent][key] = val
        else:
            parent = None
            parent_indent = -1
            values[key] = val

    return tool, values


def _coerce(value: Any, schema: dict[str, Any]) -> Any:
    typ = schema.get("type")
    if isinstance(value, dict):
        return value if typ == "object" else None
    if not isinstance(value, str):
        return value
    val = value.strip().strip("\"'")
    if typ == "integer":
        cleaned = re.sub(r"[^\d-]", "", val)
        if not cleaned or cleaned == "-":
            return None
        try:
            return int(cleaned)
        except Exception:
            return None
    if typ == "array":
        if isinstance(schema.get("items"), dict):
            return None  # array-of-object 之类，交给上层放弃
        parts = [x.strip() for x in re.split(r"[,，、]", val) if x.strip()]
        return parts or None
    if typ == "object":
        return None
    if typ == "string":
        if schema.get("enum") and val not in schema["enum"]:
            for cand in schema["enum"]:
                if cand in val:
                    return cand
            return None
        return val or None
    return val or None


def _filter_by_schema(values: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    props = schema.get("properties")
    if not isinstance(props, dict):
        return {}

    intent = values.get("expression_intent")
    if isinstance(intent, dict):
        for sub_key in list(intent.keys()):
            if sub_key in props and sub_key != "expression_intent" and sub_key not in values:
                values[sub_key] = intent.pop(sub_key)

    out: dict[str, Any] = {}
    for key, raw in values.items():
        spec = props.get(key)
        if not isinstance(spec, dict):
            continue
        if key == "msg_id":
            text = str(raw)
            normalized = text.strip().strip('"\'')
            m = _MSG_ID_STRICT.search(text) or re.search(r"^(-?\d+)$", normalized)
            if m:
                out["msg_id"] = m.group(1)
            continue
        if spec.get("type") == "object" and isinstance(raw, dict):
            sub_props = spec.get("properties") or {}
            sub_out = {}
            for sub_key, sub_val in raw.items():
                sub_spec = sub_props.get(sub_key)
                if not isinstance(sub_spec, dict):
                    continue
                coerced = _coerce(sub_val, sub_spec)
                if coerced not in (None, "", [], {}):
                    sub_out[sub_key] = coerced
            if sub_out:
                out[key] = sub_out
            continue
        coerced = _coerce(raw, spec)
        if coerced not in (None, "", [], {}):
            out[key] = coerced
    return out


def _required_keys(schema: dict[str, Any]) -> list[str]:
    raw = schema.get("required")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw if isinstance(x, str)]
    return []


def _fill_required(
    args: dict[str, Any], values: dict[str, Any], schema: dict[str, Any], synth_reference: bool
) -> dict[str, Any]:
    props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
    for key in _required_keys(schema):
        if key in args:
            continue
        if key == "msg_id":
            m = _MSG_ID_STRICT.search(str(values.get("msg_id") or ""))
            if m:
                args["msg_id"] = m.group(1)
            continue
        if key == "reply_reference" and synth_reference and isinstance(props.get("reply_reference"), dict):
            pieces = []
            intent = values.get("expression_intent") if isinstance(values.get("expression_intent"), dict) else {}
            for label, src in (
                ("focus", intent.get("focus")),
                ("reply_act", intent.get("reply_act")),
                ("tone", intent.get("tone")),
                ("scene", intent.get("scene")),
            ):
                if src:
                    pieces.append("%s: %s" % (label, src))
            if values.get("reply_reference"):
                pieces.insert(0, str(values["reply_reference"]))
            if pieces:
                args["reply_reference"] = "；".join(pieces)
    return args


# ---------------------------------------------------------------- 插件本体


class PlannerReplyFixer(MaiBotPlugin):
    """planner 输出兜底。"""

    config_model = PlannerReplyFixerConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._turns: dict[str, tuple[list[dict[str, Any]], list[Any], float]] = {}

    async def on_load(self) -> None:
        try:
            cfg = self.config.fixer
            self.ctx.logger.info(
                "[planner-fixer] 已加载，工具白名单=%s 散文模式=%s dry_run=%s",
                list(cfg.tool_names or []),
                cfg.prose_mode,
                cfg.dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[planner-fixer] 读取配置失败: %r", exc)

    async def on_unload(self) -> None:
        self._turns.clear()
        return None

    async def on_config_update(self, *args: Any, **kwargs: Any) -> None:
        self._turns.clear()
        self.ctx.logger.info("[planner-fixer] 配置已更新")

    # ---- 本轮工具表 + 上下文：只在 before_request 拿得到 ----

    @HookHandler(
        HOOK_BEFORE,
        name="planner_turn_cache",
        description="缓存本轮的可用工具与该轮上下文（after_response 只拿得到输出，拿不到上下文）",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=0,
        error_policy=ErrorPolicy.SKIP,
    )
    async def remember_turn(self, **kwargs: Any) -> None:
        try:
            session_id = str(kwargs.get("session_id") or "")
            if not session_id:
                return None
            defs = _normalize_definitions(kwargs.get("tool_definitions"))
            items = kwargs.get("items")
            ctx_items = items if isinstance(items, list) else []
            now = time.time()
            self._turns[session_id] = (defs, ctx_items, now)
            self._prune(now)
            self.ctx.logger.info(
                "[planner-fixer] 本轮工具 %d 个：%s，上下文 %d 条",
                len(defs),
                [d["name"] for d in defs][:12],
                len(ctx_items),
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[planner-fixer] 缓存本轮信息失败: %r", exc)
        return None

    def _prune(self, now: float) -> None:
        for key in [k for k, v in self._turns.items() if now - v[2] > _TURN_TTL]:
            self._turns.pop(key, None)
        overflow = len(self._turns) - _TURN_MAX
        if overflow > 0:
            oldest = sorted(self._turns.items(), key=lambda kv: kv[1][2])[:overflow]
            for key, _ in oldest:
                self._turns.pop(key, None)

    def _turn_for(self, session_id: str):
        entry = self._turns.get(session_id)
        if not entry:
            return None
        if time.time() - entry[2] > _TURN_TTL:
            self._turns.pop(session_id, None)
            return None
        return entry


    # ---- 选工具 ----

    def _allowed(self) -> list[str]:
        try:
            return [str(x).strip() for x in (self.config.fixer.tool_names or []) if str(x).strip()]
        except Exception:
            return []

    def _pick_tool(self, hinted: str, defs: list[dict[str, Any]], prose: bool) -> dict[str, Any] | None:
        allowed = self._allowed()
        if not allowed:
            return None
        allowed_lower = {a.lower(): a for a in allowed}
        by_name = {d["name"].lower(): d for d in defs}

        if prose:
            want = str(getattr(self.config.fixer, "prose_tool_name", "reply") or "reply").strip().lower()
            if want not in allowed_lower:
                if len(allowed) == 1:
                    want = allowed[0].lower()
                else:
                    return None
            return by_name.get(want)

        hint = (hinted or "").strip().lower()
        if not hint:
            if len(allowed) == 1:
                return by_name.get(allowed[0].lower())
            return None
        if hint not in allowed_lower:
            return None
        return by_name.get(hint)

    @HookHandler(
        HOOK_AFTER,
        name="planner_reply_fixer",
        description="把 planner 的纯文字『要调用工具』说明还原成真实工具调用",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=0,
        error_policy=ErrorPolicy.SKIP,
    )
    async def fix_planner_output(self, **kwargs: Any) -> dict[str, Any] | None:
        try:
            cfg = self.config.fixer
            if not cfg.enabled or not cfg.enabled_spec_parsing:
                return None

            output_items = kwargs.get("output_items")
            if not isinstance(output_items, list) or not output_items:
                return None
            if _has_tool_call(output_items):
                return None

            session_id = str(kwargs.get("session_id") or "")
            turn = self._turn_for(session_id)
            if turn is None:
                self.ctx.logger.debug("[planner-fixer] 本轮没有缓存（非 planner 轮次？），跳过")
                return None
            defs, ctx_items, _ = turn
            if not defs:
                self.ctx.logger.debug("[planner-fixer] 本轮工具列表为空，跳过")
                return None

            text = "\n".join(
                filter(
                    None,
                    (
                        _item_text(it)
                        for it in output_items
                        if isinstance(it, dict) and it.get("item_type") == "AssistantMessageItem"
                    ),
                )
            ).strip()
            if not text:
                self.ctx.logger.debug("[planner-fixer] 本轮没有 assistant 正文，跳过")
                return None
            if len(text) < int(cfg.min_text_chars):
                return None

            if _SILENCE_STRONG.search(text):
                self.ctx.logger.info("[planner-fixer] 命中强静默表述，跳过")
                return None
            if _NEG_INTENT.search(text) and not _EXPLICIT_REPLY.search(text):
                self.ctx.logger.info("[planner-fixer] 命中否定语义（不需要回复），跳过")
                return None
            if _SILENCE_WEAK.search(text) and not _reply_intent(text):
                self.ctx.logger.info("[planner-fixer] 命中弱否定且没有回复意图，跳过")
                return None
            for kw in list(cfg.silence_keywords or []):
                kw = str(kw).strip()
                if kw and kw.lower() in text.lower():
                    self.ctx.logger.info("[planner-fixer] 命中静默关键词 %s，跳过", kw)
                    return None

            hinted, spec_values = _parse_spec(text)
            prose = False
            if not spec_values:
                if not (cfg.prose_mode and _reply_intent(text)):
                    self.ctx.logger.debug("[planner-fixer] 没解析到参数结构且无回复意图，跳过")
                    return None
                ctx_msg_id = _find_msg_id_in_context(ctx_items)
                if not ctx_msg_id:
                    self.ctx.logger.info("[planner-fixer] 散文带回复意图，但上下文里没有 msg_id，放弃")
                    return None
                spec_values = {"msg_id": ctx_msg_id}
                prose = True

            tool = self._pick_tool(hinted, defs, prose)
            if not tool:
                self.ctx.logger.info(
                    "[planner-fixer] 没有可用的白名单工具（点名=%s 白名单=%s），放弃",
                    hinted or "<无>",
                    self._allowed(),
                )
                return None

            schema = tool.get("parameters") or {}
            source_values = dict(spec_values) if not prose else {"msg_id": spec_values["msg_id"]}
            args = _filter_by_schema(deepcopy(source_values), schema)
            if not prose:
                args = _fill_required(args, deepcopy(spec_values), schema, bool(cfg.synth_reply_reference))

            props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            if "reply_reference" in props and not args.get("reply_reference"):
                if prose:
                    args["reply_reference"] = _build_reference(text, int(cfg.max_reference_chars))
                elif cfg.synth_reply_reference:
                    pass
                if not args.get("reply_reference"):
                    self.ctx.logger.info("[planner-fixer] 缺 reply_reference，放弃兜底（tool=%s）", tool["name"])
                    return None

            missing = [k for k in _required_keys(schema) if k not in args]
            if missing:
                self.ctx.logger.info("[planner-fixer] 缺必填参数 %s，放弃兜底（tool=%s）", missing, tool["name"])
                return None
            if not args:
                return None

            turn_id = None
            for item in output_items:
                meta = item.get("meta") if isinstance(item, dict) else None
                if isinstance(meta, dict) and meta.get("logical_turn_id"):
                    turn_id = str(meta["logical_turn_id"])
                    break
            if not turn_id:
                self.ctx.logger.info("[planner-fixer] 本回合没有 logical_turn_id，放弃兜底")
                return None

            if cfg.dry_run:
                self.ctx.logger.info(
                    "[planner-fixer][dry-run] 命中 tool=%s 键=%s", tool["name"], ",".join(sorted(args.keys()))
                )
                return None

            call_item = {
                "item_type": "FunctionCallItem",
                "meta": {
                    "item_id": str(uuid.uuid4()),
                    "logical_turn_id": turn_id,
                    "timestamp": datetime.now().isoformat(),
                },
                "tool_call": {
                    "call_id": "call_" + uuid.uuid4().hex[:24],
                    "func_name": tool["name"],
                    "args": args,
                    "extra_content": {},
                },
            }
            kept = [
                it
                for it in output_items
                if isinstance(it, dict) and it.get("item_type") != "AssistantMessageItem"
            ]
            self.ctx.logger.info(
                "[planner-fixer] 兜底还原为 %s（键=%s，来源=%s）",
                tool["name"],
                ",".join(sorted(args.keys())),
                "散文" if prose else "参数表",
            )
            return {"modified_kwargs": {**kwargs, "output_items": kept + [call_item]}}
        except Exception as exc:  # noqa: BLE001
            try:
                self.ctx.logger.warning("[planner-fixer] 兜底失败，保留原始输出: %r", exc)
            except Exception:  # noqa: BLE001
                pass
            return None


def create_plugin() -> PlannerReplyFixer:
    """宿主加载入口。"""
    return PlannerReplyFixer()
