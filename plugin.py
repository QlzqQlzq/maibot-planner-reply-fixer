"""planner 决策兜底插件（小模型决策辅助）。

背景：小参数量模型在 planner 阶段经常把「要调用的工具」写成一段文字说明，例如：

    决策：调用reply工具，让希罗发出一条温和的简短回应。
    调用reply工具：
    - msg_id: 使用当前最新消息 msg_id="-123456"
    - expression_intent:
      - tone: 温和、柔和
      - scene: 关怀回应
      - focus: A裘千仞的「摸摸希罗」请求
      - reply_act: 确认关怀、柔和回应
      - reply_style: 正常回复

它并没有真的发起 tool call，宿主于是判定「无调用」，角色什么都不说。

本插件的做法（**解析真实参数**，不自己编台词）：
1. 从这段文字里解析出「- key: value」结构（支持一层嵌套）；
2. 用平台已注册工具的真实 parameters_schema 过滤字段、收敛类型、校验必填；
3. 组装成真正的 FunctionCallItem —— 台词仍由宿主 replayer 生成。

关键约束：
- **不伪造工具**：工具名与参数名全部来自 `ctx.tool.get_definitions()`。
- **不编台词**：只有该工具确实声明了文本参数时，才从引号里取台词；
  否则一律不猜（例如麦麦的 reply 工具没有台词参数，台词由 replayer 生成）。
- 尊重「不说话」：命中静默信号直接放弃。
- 失败即放行：任何异常都吞掉返回 None。
"""

import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any

from maibot_sdk import Field, HookHandler, MaiBotPlugin, PluginConfigBase
from maibot_sdk.types import ErrorPolicy, HookMode, HookOrder

HOOK_NAME = "maisaka.planner.after_response"
_DEFS_TTL_SECONDS = 60.0

# 「调用 xxx 工具」的说明句
_SPEC_HEAD = re.compile(r"调用\s*([A-Za-z_][A-Za-z0-9_]*)\s*工具|call\s+([A-Za-z_][A-Za-z0-9_]*)\s+tool", re.I)
# 「- key: value」列表项（含中文冒号）
_SPEC_ITEM = re.compile(r"^(?P<indent>\s*)[-*]\s*(?P<key>[A-Za-z_][A-Za-z0-9_]*)\s*[:：]\s*(?P<val>.*)$")
# 从文字里捞 msg_id
_MSG_ID = re.compile(r"msg_id\s*[=:：]\s*[\"']?(-?\d{4,})[\"']?", re.I)
# 引号台词（仅当工具真声明文本参数时才用）
_QUOTE_RE = re.compile(r"[「“\"']([^「」“”\"'（）()]{2,200})[」”\"']")
_SENT_SPLIT = re.compile(r"[。！？!?\n]")


class PluginSectionConfig(PluginConfigBase):
    """插件元信息段。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用本插件")
    config_version: str = Field(default="0.1.5", description="配置版本")


class FixerConfig(PluginConfigBase):
    """兜底行为配置。"""

    __ui_label__ = "决策兜底"
    __ui_icon__ = "wrench"
    __ui_order__ = 1

    enabled: bool = Field(default=True, description="是否启用兜底")
    models: list[str] = Field(
        default_factory=lambda: ["spark-x2.6-4b"],
        description="只对这些 planner 模型生效；留空表示不限制",
    )
    tool_names: list[str] = Field(
        default_factory=lambda: ["reply"],
        description="允许兜底的工具名（必须是已注册工具，按顺序匹配）",
    )
    enabled_spec_parsing: bool = Field(
        default=True,
        description="是否解析「调用xxx工具：- key: value」这类文字说明并还原成真实调用",
    )
    synth_reply_reference: bool = Field(
        default=True,
        description="缺少 reply_reference 时，用解析到的 focus/reply_act/tone 拼一句上下文说明补上",
    )
    text_arg_names: list[str] = Field(
        default_factory=lambda: ["text", "content", "message", "msg", "reply", "reply_text"],
        description="仅当工具真的声明了文本参数时，才用它承接从引号里提取的台词",
    )
    dry_run: bool = Field(default=False, description="只写日志、不改写输出（观察用）")
    min_text_chars: int = Field(default=20, ge=0, le=2000, description="模型输出短于该长度时忽略")
    max_reply_chars: int = Field(default=120, ge=10, le=500, description="提取台词时的最大字数")
    silence_keywords: list[str] = Field(
        default_factory=lambda: [
            "不进行工具调用",
            "不调用",
            "保持静默",
            "保持沉默",
            "无需回复",
            "不需要回复",
            "不回复",
            "不发言",
            "不必回复",
            "wait",
        ],
        description="判定『模型决定静默』的否定信号（命中即放弃兜底）",
    )


class PlannerReplyFixerConfig(PluginConfigBase):
    """插件根配置。"""

    plugin: PluginSectionConfig = Field(default_factory=PluginSectionConfig)
    fixer: FixerConfig = Field(default_factory=FixerConfig)


# ---------------------------------------------------------------- 工具函数


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
    1. hook 里的 tool_definitions（OpenAI 风格）：
       {"type": "function", "function": {"name": ..., "parameters": {...}}}
    2. 能力 tool.get_definitions 的返回：
       {"success": true, "tools": [{"name": ..., "definition": {"name", "description", "parameters_schema"}}]}
       （SDK 也可能只把 tools 解包成列表交给插件）
    3. {"name": ..., "parameters": {...}} / {"name": ..., "parameters_schema": {...}}
    """
    out: list[dict[str, Any]] = []

    def _walk(node: Any) -> None:
        if isinstance(node, dict):
            if node.get("success") is not None and isinstance(node.get("tools"), list):
                for sub in node["tools"]:
                    _walk(sub)
                return
            if isinstance(node.get("tools"), list):
                for sub in node["tools"]:
                    _walk(sub)
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
            if isinstance(name, str) and name:
                out.append({"name": name, "parameters": params if isinstance(params, dict) else {}})
            return
        if isinstance(node, list):
            for sub in node:
                _walk(sub)

    _walk(raw)
    return out


def _parse_spec(text: str) -> tuple[str, dict[str, Any]]:
    """解析「调用 X 工具：- key: value」结构，返回 (工具名, 参数字典)。"""
    tool = ""
    m = _SPEC_HEAD.search(text)
    if m:
        tool = (m.group(1) or m.group(2) or "").strip()

    values: dict[str, Any] = {}
    current_parent = None
    parent_indent = -1

    for line in text.splitlines():
        item = _SPEC_ITEM.match(line)
        if not item:
            if current_parent is not None and line.strip() and not line.strip().startswith(("-", "*")):
                target = values.get(current_parent)
                if isinstance(target, str) and target:
                    values[current_parent] = target + " " + line.strip()
            continue

        raw_indent = len(item.group("indent").replace("\t", "    "))
        key = item.group("key")
        val = item.group("val").strip()

        if not val:
            current_parent = key
            parent_indent = raw_indent
            values.setdefault(key, {})
            continue

        if current_parent is not None and raw_indent > parent_indent and isinstance(values.get(current_parent), dict):
            values[current_parent][key] = val
        else:
            current_parent = None
            parent_indent = -1
            values[key] = val

    mid = _MSG_ID.search(text)
    if mid and not values.get("msg_id"):
        values["msg_id"] = mid.group(1)

    return tool, values


def _coerce(value: Any, schema: dict[str, Any]) -> Any:
    """按 schema 做最小限度的类型转换；不合适返回 None。"""
    typ = schema.get("type")
    if isinstance(value, dict):
        return value if typ == "object" else None
    if not isinstance(value, str):
        return value
    val = value.strip().strip("\"'")
    if typ == "integer":
        try:
            return int(re.sub(r"[^\d-]", "", val) or 0)
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
    """只保留 schema 里声明过的字段，并做类型/枚举收敛。"""
    props = schema.get("properties")
    if not isinstance(props, dict):
        return {}

    # 模型常把 reply_style 之类写进 expression_intent 里，这里提升到顶层
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
            # msg_id 必须是纯 id：从「使用当前消息 msg_id="-123"」这类句子里抠出来
            mid = _MSG_ID.search(str(raw)) or re.search(r"(-?\d{4,})", str(raw))
            if mid:
                out["msg_id"] = mid.group(1)
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


def _fill_required(
    args: dict[str, Any], values: dict[str, Any], schema: dict[str, Any], synth_reference: bool
) -> dict[str, Any]:
    """补齐必填项（缺 reply_reference 时用解析到的上下文拼一句说明）。"""
    required = schema.get("required")
    if not isinstance(required, list):
        return args
    for key in required:
        if key in args:
            continue
        if key == "msg_id":
            mid = _MSG_ID.search(str(values.get("msg_id") or ""))
            if mid:
                args["msg_id"] = mid.group(1)
            continue
        if key == "reply_reference" and synth_reference:
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


def _extract_quote_line(text: str, limit: int) -> str:
    spans = [s.strip() for s in _QUOTE_RE.findall(text) if s.strip()]
    if spans:
        return spans[-1][:limit]
    for sentence in reversed([s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]):
        if any(k in sentence for k in ("决策", "分析", "调用", "工具", "因此")):
            continue
        cleaned = re.sub(r"^[因此所以综上，,：:\s]+", "", sentence)
        if len(cleaned) >= 2:
            return cleaned[:limit]
    return ""


def _pick_text_arg(tool: dict[str, Any], candidates: list[str]) -> str | None:
    """只有工具真声明了文本参数时才返回参数名。"""
    props = (tool.get("parameters") or {}).get("properties")
    if not isinstance(props, dict) or not props:
        return None
    for cand in candidates:
        spec = props.get(cand)
        if isinstance(spec, dict) and spec.get("type") == "string" and not spec.get("enum"):
            return cand
    return None


# ---------------------------------------------------------------- 插件本体


class PlannerReplyFixer(MaiBotPlugin):
    """planner 输出兜底。"""

    config_model = PlannerReplyFixerConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._defs_cache: list[dict[str, Any]] = []
        self._defs_at = 0.0
        # 每轮 before_request 里拿到的工具定义（session_id -> (定义, 时间)）
        self._turn_defs: dict[str, tuple[list[dict[str, Any]], float]] = {}

    async def on_load(self) -> None:
        cfg = self.config.fixer
        self.ctx.logger.info(
            "[planner-fixer] 已加载，模型名单=%s 工具名=%s spec解析=%s dry_run=%s",
            list(getattr(cfg, "models", []) or []),
            list(getattr(cfg, "tool_names", []) or []),
            getattr(cfg, "enabled_spec_parsing", True),
            getattr(cfg, "dry_run", False),
        )

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, *args: Any, **kwargs: Any) -> None:
        self._defs_cache = []
        self._defs_at = 0.0
        self._turn_defs.clear()
        self.ctx.logger.info("[planner-fixer] 配置已更新")

    async def _model_in_scope(self) -> bool:
        models = [m.strip() for m in (getattr(self.config.fixer, "models", None) or []) if m and m.strip()]
        if not models:
            return True
        try:
            planner_models = await self.ctx.config.get("model_task_config.planner.model_list", [])
        except Exception as exc:  # noqa: BLE001
            # 读不到就不生效，避免「只对小模型」悄悄退化成全局生效
            self.ctx.logger.warning("[planner-fixer] 读取宿主模型配置失败，本轮不兜底: %r", exc)
            return False
        if not isinstance(planner_models, list):
            self.ctx.logger.warning("[planner-fixer] planner 模型配置格式异常，本轮不兜底")
            return False
        return bool({str(m).strip() for m in planner_models} & set(models))

    async def _tool_definitions(self) -> list[dict[str, Any]]:
        now = time.time()
        if self._defs_cache and now - self._defs_at < _DEFS_TTL_SECONDS:
            return self._defs_cache
        try:
            raw = await self.ctx.tool.get_definitions()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[planner-fixer] 获取工具定义失败: %r", exc)
            return []
        self._defs_cache = _normalize_definitions(raw)
        self._defs_at = now
        return self._defs_cache

    def _defs_for_turn(self, session_id: str) -> list[dict[str, Any]]:
        """取本轮 before_request 缓存的工具定义（含宿主内置工具）。"""
        entry = self._turn_defs.get(session_id)
        if not entry:
            return []
        defs, stamp = entry
        if time.time() - stamp > 300:
            self._turn_defs.pop(session_id, None)
            return []
        return defs

    @HookHandler(
        "maisaka.planner.before_request",
        name="planner_tools_cache",
        description="缓存本轮 planner 可用的工具定义，供 after_response 还原调用时使用",
        mode=HookMode.OBSERVE,
        order=HookOrder.NORMAL,
        timeout_ms=2000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def remember_tool_definitions(self, **kwargs: Any) -> None:
        try:
            defs = _normalize_definitions(kwargs.get("tool_definitions"))
            session_id = str(kwargs.get("session_id") or "")
            if defs and session_id:
                self._turn_defs[session_id] = (defs, time.time())
                self.ctx.logger.debug(
                    "[planner-fixer] 缓存本轮工具定义 %d 个: %s",
                    len(defs),
                    [d["name"] for d in defs][:12],
                )
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.debug("[planner-fixer] 缓存工具定义失败: %r", exc)
        return None

    async def _pick_tool(self, hinted: str, session_id: str = "") -> dict[str, Any] | None:
        cfg = self.config.fixer
        # 优先用本轮 before_request 的真实工具定义（内置工具也在里面）
        defs = self._defs_for_turn(session_id)
        if not defs:
            defs = await self._tool_definitions()
        if not defs:
            return None
        by_name = {}
        for d in defs:
            by_name.setdefault(d["name"], d)
        for want in [hinted] + list(cfg.tool_names or []):
            name = str(want or "").strip()
            if name and name in by_name:
                return by_name[name]
        return None

    @HookHandler(
        HOOK_NAME,
        name="planner_reply_fixer",
        description="把 planner 的纯文字『要调用工具』说明还原成真实工具调用",
        mode=HookMode.BLOCKING,
        order=HookOrder.NORMAL,
        timeout_ms=3000,
        error_policy=ErrorPolicy.SKIP,
    )
    async def fix_planner_output(self, **kwargs: Any) -> dict[str, Any] | None:
        cfg = self.config.fixer
        try:
            if not cfg.enabled:
                return None

            items = kwargs.get("output_items")
            if not isinstance(items, list) or not items:
                return None
            if _has_tool_call(items):
                return None

            text = "\n".join(filter(None, (_item_text(it) for it in items))).strip()
            if len(text) < int(cfg.min_text_chars):
                return None

            lowered = text.lower()
            hit_silence = [k for k in (cfg.silence_keywords or []) if k.lower() in lowered]
            if hit_silence:
                self.ctx.logger.debug("[planner-fixer] 命中静默信号 %s，跳过", hit_silence)
                return None

            if not await self._model_in_scope():
                return None

            hinted_tool, spec_values = _parse_spec(text) if cfg.enabled_spec_parsing else ("", {})
            if not hinted_tool and not spec_values:
                self.ctx.logger.debug("[planner-fixer] 未发现工具调用说明，跳过")
                return None

            tool = await self._pick_tool(hinted_tool, str(kwargs.get("session_id") or ""))
            if not tool:
                self.ctx.logger.info("[planner-fixer] 没有可用的已注册工具（hint=%s），放弃兜底", hinted_tool)
                return None

            schema = tool.get("parameters") or {}
            args = _filter_by_schema(spec_values, schema)
            args = _fill_required(args, spec_values, schema, bool(cfg.synth_reply_reference))

            if not args:
                text_arg = _pick_text_arg(tool, list(cfg.text_arg_names or []))
                line = _extract_quote_line(text, int(cfg.max_reply_chars)) if text_arg else ""
                if line:
                    args = {text_arg: line}
                else:
                    self.ctx.logger.info("[planner-fixer] 解析到的参数不足，放弃兜底（tool=%s）", tool["name"])
                    return None

            required = schema.get("required") if isinstance(schema.get("required"), list) else []
            missing = [k for k in required if k not in args]
            if missing:
                self.ctx.logger.info("[planner-fixer] 缺少必填参数 %s，放弃兜底（tool=%s）", missing, tool["name"])
                return None

            if cfg.dry_run:
                self.ctx.logger.info("[planner-fixer][dry-run] 命中 tool=%s args=%s", tool["name"], args)
                return None

            turn_id = None
            for item in items:
                meta = item.get("meta") if isinstance(item, dict) else None
                if isinstance(meta, dict) and meta.get("logical_turn_id"):
                    turn_id = str(meta["logical_turn_id"])
                    break

            call_item = {
                "item_type": "FunctionCallItem",
                "meta": {
                    "item_id": str(uuid.uuid4()),
                    "logical_turn_id": turn_id,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                "tool_call": {
                    "call_id": "call_" + uuid.uuid4().hex[:24],
                    "func_name": tool["name"],
                    "args": args,
                    "extra_content": {},
                },
            }
            self.ctx.logger.info("[planner-fixer] 兜底还原为 %s(%s)", tool["name"], args)
            return {"modified_kwargs": {**kwargs, "output_items": [call_item]}}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[planner-fixer] 兜底失败，保留原始输出: %r", exc)
            return None


def create_plugin() -> PlannerReplyFixer:
    """宿主加载入口。"""
    return PlannerReplyFixer()
