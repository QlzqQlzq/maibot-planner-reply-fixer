"""planner 决策兜底插件（小模型决策辅助）。

背景：小参数量模型在 planner 阶段经常把「要调用的工具」写成一段文字说明，例如：

    决策：调用reply工具，让希罗发出一条温和的简短回应。
    调用reply工具：
    - msg_id: 使用当前最新消息 msg_id="-123456"
    - expression_intent:
      - tone: 温和、柔和
      - focus: 对方的「摸摸头」请求
      - reply_style: 正常回复

它并没有真的发起 tool call，宿主于是判定「无调用」，角色什么都不说。
本插件把这段文字按真实参数还原成一次工具调用；台词仍由宿主 replayer 生成。

安全约定（都是踩过的坑）：
1. 工具名必须落在配置的 `tool_names` 白名单里。模型散文里点名的其它工具一律不认，
   否则一句「调用 send_group_msg 工具」就能让它替你发消息。
2. 工具定义只来自宿主每轮通过 `maisaka.planner.before_request` 交给我们的
   `tool_definitions`（含内置 reply）。拿不到就当本轮不做，不猜、不回退。
3. 参数只保留工具 schema 声明过的字段；`reply_reference` 这类必填缺失就放弃。
4. `logical_turn_id` 必须非空（宿主 context_item 会校验），取不到就放弃。
5. 回写时保留原来的 Reasoning / Provider Activity 项，只把 AssistantMessageItem 换成调用项，
   避免把「为什么这么回」的推理从历史里抹掉。
6. 模型明确表示不说话时直接跳过；任何异常都吞掉返回原样。
"""

import re
import time
import uuid
from datetime import datetime
from typing import Any

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

HOOK_BEFORE = "maisaka.planner.before_request"
HOOK_AFTER = "maisaka.planner.after_response"
_TURN_DEFS_TTL = 300.0
_TURN_DEFS_MAX = 128

# 「调用 xxx 工具」的说明句
_SPEC_HEAD = re.compile(r"调用\s*([A-Za-z_][A-Za-z0-9_]*)\s*工具|call\s+([A-Za-z_][A-Za-z0-9_]*)\s+tool", re.I)
# 「- key: value」列表项（中英文冒号都认）
_SPEC_ITEM = re.compile(r"^(?P<indent>[ \t]*)[-*]\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*[:：]\s*(?P<val>.*)$")
# msg_id：只认 `msg_id=123` / `msg_id: 123` 这种，避免把引用里的数字当目标
_MSG_ID_STRICT = re.compile(r"msg_id\s*[=:：]\s*[\"']?(-?\d+)[\"']?", re.I)

# 内置静默判断（不可配置，避免把安全阀关掉）
_SILENCE_BUILTIN = re.compile(
    r"不进行工具调用|不进行任何工具调用|不调用(?:任何)?(?:工具)?|无需调用|不要调用|"
    r"保持静默|保持沉默|保持安静|不说话|暂不发言|不做任何操作|"
    r"do\s*not\s+call(?:\s+any)?\s+tool|don'?t\s+call(?:\s+any)?\s+tool|stay\s+silent|no\s+tool\s+call",
    re.I,
)


class PluginSectionConfig(PluginConfigBase):
    """插件元信息段（宿主校验 config_version 用）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="宿主侧插件开关（该字段由宿主维护）")
    config_version: str = Field(default="0.1.6", description="配置版本")


class FixerConfig(PluginConfigBase):
    """兜底行为配置。"""

    __ui_label__ = "决策兜底"
    __ui_icon__ = "wrench"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="兜底开关")
    models: list[str] = Field(
        default_factory=list,
        description="只对这些 planner 模型生效；留空表示不限制（宿主 planner 随机选模型时建议留空）",
    )
    tool_names: list[str] = Field(
        default_factory=lambda: ["reply"],
        description="允许兜底的工具白名单。模型散文里点到别的工具一律不认",
    )
    enabled_spec_parsing: bool = Field(
        default=True,
        description="是否解析「调用xxx工具：- key: value」这类文字说明",
    )
    synth_reply_reference: bool = Field(
        default=True,
        description="缺少 reply_reference 时，用 focus / reply_act / tone / scene 拼一句上下文说明",
    )
    dry_run: bool = Field(default=False, description="只写日志、不改写输出（观察用）")
    min_text_chars: int = Field(default=20, ge=0, le=2000, description="模型输出短于该长度时忽略")
    silence_keywords: list[str] = Field(
        default_factory=list,
        description="额外的静默关键词（内置静默判断始终生效，这里只做补充）",
    )


class PlannerReplyFixerConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    fixer: FixerConfig = Field(default_factory=FixerConfig)


# ---------------------------------------------------------------- 解析工具


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


def _normalize_definitions(raw: Any) -> list[dict[str, Any]]:
    """把宿主给的几种工具定义形态统一成 [{name, parameters}]。

    兼容：
    1. hook 里 tool_definitions（OpenAI 风格）：{"type":"function","function":{"name","parameters"}}
    2. 能力 tool.get_definitions 返回：{"success":true,"tools":[{"name","definition":{"parameters_schema"}}]}
    3. {"name":...,"parameters":{...}} / {"name":...,"parameters_schema":{...}}
    """
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
    """解析「- key: value」结构，返回 (文本里点名的工具, 参数字典)。"""
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
    """按 schema 做最小类型转换；不合适返回 None（不猜、不补 0）。"""
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
        parts = [x.strip() for x in re.split(r"[,，、]", val) if x.strip()]
        return parts or None
    if typ == "string":
        if schema.get("enum") and val not in schema["enum"]:
            for cand in schema["enum"]:
                if cand in val:
                    return cand
            return None
        return val or None
    return val or None


def _filter_by_schema(values: dict[str, Any], schema: dict[str, Any]) -> dict[str, Any]:
    """只保留 schema 声明过的字段，并做类型/枚举收敛。"""
    props = schema.get("properties")
    if not isinstance(props, dict):
        return {}

    # 模型常把 reply_style 之类写进 expression_intent 里，提到顶层
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
            normalized = text.strip().strip('"')
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
    """兼容 required 是 list / str。"""
    raw = schema.get("required")
    if isinstance(raw, str):
        return [raw]
    if isinstance(raw, list):
        return [str(x) for x in raw if isinstance(x, str)]
    return []


def _fill_required(
    args: dict[str, Any], values: dict[str, Any], schema: dict[str, Any], synth_reference: bool
) -> dict[str, Any]:
    """补齐必填项：msg_id 从文本抠，reply_reference 用解析到的上下文拼。"""
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
        self._turn_defs: dict[str, tuple[list[dict[str, Any]], float]] = {}

    async def on_load(self) -> None:
        try:
            cfg = self.config.fixer
            self.ctx.logger.info(
                "[planner-fixer] 已加载，模型名单=%s 工具白名单=%s spec解析=%s dry_run=%s",
                list(cfg.models or []) or "不限制",
                list(cfg.tool_names or []),
                cfg.enabled_spec_parsing,
                cfg.dry_run,
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[planner-fixer] 读取配置失败: %r", exc)

    async def on_unload(self) -> None:
        self._turn_defs.clear()
        return None

    async def on_config_update(self, *args: Any, **kwargs: Any) -> None:
        self._turn_defs.clear()
        self.ctx.logger.info("[planner-fixer] 配置已更新")

    # ---- 工具定义：只认宿主每轮交过来的那份 ----

    @HookHandler(
        HOOK_BEFORE,
        name="planner_tools_cache",
        description="缓存本轮 planner 可用的工具定义，供 after_response 还原调用时使用",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=0,
        error_policy=ErrorPolicy.SKIP,
    )
    async def remember_tool_definitions(self, **kwargs: Any) -> None:
        try:
            session_id = str(kwargs.get("session_id") or "")
            if not session_id:
                return None
            defs = _normalize_definitions(kwargs.get("tool_definitions"))
            now = time.time()
            self._turn_defs[session_id] = (defs, now)
            self._prune(now)
            self.ctx.logger.info(
                "[planner-fixer] 本轮工具 %d 个：%s", len(defs), [d["name"] for d in defs][:12]
            )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[planner-fixer] 缓存工具定义失败: %r", exc)
        return None

    def _prune(self, now: float) -> None:
        for key in [k for k, (_, ts) in self._turn_defs.items() if now - ts > _TURN_DEFS_TTL]:
            self._turn_defs.pop(key, None)
        overflow = len(self._turn_defs) - _TURN_DEFS_MAX
        if overflow > 0:
            oldest = sorted(self._turn_defs.items(), key=lambda kv: kv[1][1])[:overflow]
            for key, _ in oldest:
                self._turn_defs.pop(key, None)

    def _defs_for_turn(self, session_id: str) -> list[dict[str, Any]] | None:
        """返回本轮工具定义；没缓存过返回 None（表示本轮不该动）。"""
        entry = self._turn_defs.get(session_id)
        if not entry:
            return None
        defs, stamp = entry
        if time.time() - stamp > _TURN_DEFS_TTL:
            self._turn_defs.pop(session_id, None)
            return None
        return defs

    # ---- 模型作用域 ----

    async def _model_in_scope(self) -> bool:
        try:
            models = [str(m).strip() for m in (self.config.fixer.models or []) if str(m).strip()]
        except Exception:
            return False
        if not models:
            return True
        try:
            planner_models = await self.ctx.config.get("model_task_config.planner.model_list", [])
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[planner-fixer] 读取宿主模型配置失败，本轮不兜底: %r", exc)
            return False
        if not isinstance(planner_models, list):
            self.ctx.logger.info("[planner-fixer] planner 模型配置格式异常，本轮不兜底")
            return False
        current = set()
        for item in planner_models:
            if isinstance(item, dict):
                name = item.get("model") or item.get("name")
                if name:
                    current.add(str(name).strip())
            else:
                current.add(str(item).strip())
        if current & set(models):
            return True
        self.ctx.logger.info(
            "[planner-fixer] 当前 planner 模型不在名单内（名单=%s，实际=%s），本轮不兜底",
            models,
            sorted(current),
        )
        return False

    # ---- 主链路 ----

    def _pick_allowed_tool(self, hinted: str, defs: list[dict[str, Any]]) -> dict[str, Any] | None:
        """只允许白名单里的工具；点名的工具不在白名单就不做。"""
        try:
            allowed = [str(x).strip() for x in (self.config.fixer.tool_names or []) if str(x).strip()]
        except Exception:
            return None
        if not allowed:
            return None
        allowed_lower = {a.lower() for a in allowed}
        by_name = {d["name"].lower(): d for d in defs}

        hint = (hinted or "").strip().lower()
        if hint:
            if hint not in allowed_lower:
                return None
            return by_name.get(hint)
        if len(allowed) == 1:
            return by_name.get(allowed[0].lower())
        return None

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
            if not cfg.enabled:
                return None

            items = kwargs.get("output_items")
            if not isinstance(items, list) or not items:
                return None
            if _has_tool_call(items):
                return None

            session_id = str(kwargs.get("session_id") or "")
            defs = self._defs_for_turn(session_id)
            if defs is None:
                self.ctx.logger.debug("[planner-fixer] 本轮没有工具定义缓存（非 planner 轮次？），跳过")
                return None
            if not defs:
                self.ctx.logger.debug("[planner-fixer] 本轮工具列表为空，跳过")
                return None

            text = "\n".join(filter(None, (_item_text(it) for it in items))).strip()
            if len(text) < int(cfg.min_text_chars):
                return None

            if _SILENCE_BUILTIN.search(text):
                self.ctx.logger.info("[planner-fixer] 命中内置静默规则，跳过")
                return None
            for kw in list(cfg.silence_keywords or []):
                kw = str(kw).strip()
                if kw and kw.lower() in text.lower():
                    self.ctx.logger.info("[planner-fixer] 命中静默关键词 %s，跳过", kw)
                    return None

            if not await self._model_in_scope():
                return None

            hinted, spec_values = _parse_spec(text) if cfg.enabled_spec_parsing else ("", {})
            if not spec_values:
                self.ctx.logger.debug("[planner-fixer] 没解析到参数结构，跳过")
                return None

            tool = self._pick_allowed_tool(hinted, defs)
            if not tool:
                self.ctx.logger.info(
                    "[planner-fixer] 点名的工具 %s 不在白名单 %s 内（或该轮没有这个工具），放弃",
                    hinted or "<无>",
                    list(cfg.tool_names or []),
                )
                return None

            schema = tool.get("parameters") or {}
            args = _filter_by_schema(dict(spec_values), schema)
            args = _fill_required(args, spec_values, schema, bool(cfg.synth_reply_reference))

            props = schema.get("properties") if isinstance(schema.get("properties"), dict) else {}
            if "reply_reference" in props and not args.get("reply_reference"):
                self.ctx.logger.info("[planner-fixer] 缺 reply_reference，放弃兜底（tool=%s）", tool["name"])
                return None

            missing = [k for k in _required_keys(schema) if k not in args]
            if missing:
                self.ctx.logger.info("[planner-fixer] 缺必填参数 %s，放弃兜底（tool=%s）", missing, tool["name"])
                return None

            if not args:
                self.ctx.logger.info("[planner-fixer] 参数为空，放弃兜底")
                return None

            turn_id = None
            for item in items:
                meta = item.get("meta") if isinstance(item, dict) else None
                if isinstance(meta, dict) and meta.get("logical_turn_id"):
                    turn_id = str(meta["logical_turn_id"])
                    break
            if not turn_id:
                self.ctx.logger.info("[planner-fixer] 本回合没有 logical_turn_id，放弃兜底")
                return None

            if cfg.dry_run:
                self.ctx.logger.info("[planner-fixer][dry-run] 命中 tool=%s args=%s", tool["name"], args)
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
                for it in items
                if isinstance(it, dict) and it.get("item_type") != "AssistantMessageItem"
            ]
            self.ctx.logger.info("[planner-fixer] 兜底还原为 %s(%s)", tool["name"], args)
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
