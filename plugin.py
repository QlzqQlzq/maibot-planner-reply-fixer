"""planner 决策兜底插件（小模型决策辅助）。

背景：spark-x2.6-4b 这类小模型在 planner 阶段经常只写出
「……因此决策为调用 reply，用简洁自然的口吻回应『xxx』」这样的文字，
而不是真的发起 tool call，导致 Maisaka 判定「无调用」而不回复。

做法：监听 `maisaka.planner.after_response`。当输出里没有任何工具调用、
但文字明显表达了「想回复」的意图时，**从麦麦已注册的工具里**取出对应的
回复工具（默认名字 reply），按它自己的参数 schema 填好文本，再改写成
真实的工具调用。

关键约束（重要）：
- **不自己编工具**：工具名、参数名全部来自 `ctx.tool.get_definitions()`。
  找不到匹配工具、或该工具的必需参数我们填不出来 → 直接放弃，保持原样。
- 只在配置的模型名单命中时生效（读宿主 model_task_config.planner.model_list）。
- 已经有工具调用时绝不插手。
- dry_run 可先观察不改写。
- 任何异常都吞掉并返回 None，保持宿主原有行为。
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

# 引号里的台词候选：优先取它
_QUOTE_RE = re.compile(r"[「“\"']([^「」“”\"'（）()]{2,200})[」”\"']")
_SENT_SPLIT = re.compile(r"[。！？!?\n]")
# 判定「这句是不是分析/决策本身」，是就跳过
_META_HINTS = ("决策", "分析", "理由", "调用", "工具", "判断", "因此", "综上")
# 引号里如果只是角色名/工具名，也不要
_NAME_LIKE = ("希罗", "二阶堂", "reply", "wait", "incomplete_talk", "工具", "决策")


class PluginSectionConfig(PluginConfigBase):
    """插件元信息段（宿主校验 config_version 用）。"""

    __ui_label__ = "插件"
    __ui_icon__ = "package"
    __ui_order__ = 0

    enabled: bool = Field(default=True, description="是否启用本插件")
    config_version: str = Field(default="0.1.0", description="配置版本")


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
    reply_tool_names: list[str] = Field(
        default_factory=lambda: ["reply"],
        description="回复类工具的名字（必须是麦麦已注册的工具名，按顺序匹配）",
    )
    text_arg_names: list[str] = Field(
        default_factory=lambda: ["text", "content", "message", "msg", "reply", "reply_text"],
        description="回复工具里承载正文的参数名（按顺序匹配它的 schema）",
    )
    dry_run: bool = Field(default=False, description="只写日志、不改写输出（观察用）")
    fallback_text: str = Field(default="嗯。", description="提取不到回复文本时的兜底句")
    allow_fallback: bool = Field(
        default=False,
        description="提取不到台词时是否用 fallback_text 兜底发送（默认关闭，避免误发）",
    )
    max_reply_chars: int = Field(default=120, ge=10, le=500, description="兜底回复的最大字数")
    min_text_chars: int = Field(default=20, ge=0, le=2000, description="模型输出短于该长度时忽略")
    reply_keywords: list[str] = Field(
        default_factory=lambda: [
            "调用reply",
            "调用 reply",
            "调用reply工具",
            "使用reply",
            "使用 reply",
            "reply工具",
            "reply 工具",
        ],
        description="判定『模型决定调用回复工具』的强信号（必须明确说要调用）",
    )
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


def _trim(text: str, limit: int) -> str:
    text = (text or "").strip().strip("\"'`")
    text = re.sub(r"\s+", " ", text)
    if len(text) > limit:
        text = text[:limit].rstrip()
    return text


def _item_text(item: Any) -> str:
    """取出一个 Context Item 里的文本内容。"""
    if not isinstance(item, dict):
        return ""
    parts = item.get("parts")
    if not isinstance(parts, list):
        return ""
    chunks: list[str] = []
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


def _extract_reply(text: str, limit: int, fallback: str) -> str:
    """优先取引号里的台词；其次取最后一句非决策句。"""
    spans = [s.strip() for s in _QUOTE_RE.findall(text) if s.strip()]
    spans = [s for s in spans if not any(n in s for n in _NAME_LIKE)]
    if spans:
        return _trim(spans[-1], limit) or fallback

    sentences = [s.strip() for s in _SENT_SPLIT.split(text) if s.strip()]
    for sentence in reversed(sentences):
        if any(k in sentence for k in _META_HINTS):
            continue
        cleaned = re.sub(r"^[因此所以综上，,：:\s]+", "", sentence)
        if len(cleaned) >= 2:
            return _trim(cleaned, limit)
    return fallback


def _normalize_definitions(raw: Any) -> list[dict[str, Any]]:
    """把宿主返回的工具定义整理成 [{name, parameters}]。"""
    out: list[dict[str, Any]] = []
    if not isinstance(raw, list):
        return out
    for item in raw:
        if not isinstance(item, dict):
            continue
        fn = item.get("function") if isinstance(item.get("function"), dict) else item
        name = fn.get("name")
        if not isinstance(name, str) or not name:
            continue
        params = fn.get("parameters")
        out.append({"name": name, "parameters": params if isinstance(params, dict) else {}})
    return out


def _pick_text_arg(tool: dict[str, Any], candidates: list[str]) -> str | None:
    """在这个工具自己的 schema 里找承载正文的参数名。"""
    params = tool.get("parameters") or {}
    props = params.get("properties")
    if not isinstance(props, dict) or not props:
        return None
    # 1) 按配置顺序匹配已存在的属性
    for cand in candidates:
        if cand in props:
            return cand
    # 2) 退一步：找第一个 string 类型属性
    for key, spec in props.items():
        if isinstance(spec, dict) and str(spec.get("type") or "") == "string":
            return key
    return None


class PlannerReplyFixer(MaiBotPlugin):
    """planner 输出兜底：文字 -> 麦麦真实回复工具的调用。"""

    config_model = PlannerReplyFixerConfig

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._defs_cache: list[dict[str, Any]] = []
        self._defs_at: float = 0.0

    async def on_load(self) -> None:
        cfg = self.config.fixer
        self.ctx.logger.info(
            "[planner-fixer] 已加载，模型名单=%s 工具名=%s dry_run=%s",
            list(getattr(cfg, "models", []) or []),
            list(getattr(cfg, "reply_tool_names", []) or []),
            getattr(cfg, "dry_run", False),
        )

    async def on_unload(self) -> None:
        return None

    async def on_config_update(self, *args: Any, **kwargs: Any) -> None:
        """配置热更新：清掉工具定义缓存，下一轮重新拉取。"""
        self._defs_cache = []
        self._defs_at = 0.0
        self.ctx.logger.info("[planner-fixer] 配置已更新")

    async def _model_in_scope(self) -> bool:
        models = [m.strip() for m in (getattr(self.config.fixer, "models", None) or []) if m and m.strip()]
        if not models:
            return True
        try:
            planner_models = await self.ctx.config.get("model_task_config.planner.model_list", [])
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[planner-fixer] 读取宿主模型配置失败，按生效处理: %r", exc)
            return True
        if not isinstance(planner_models, list):
            return True
        current = {str(m).strip() for m in planner_models}
        return bool(current & set(models))

    async def _tool_definitions(self) -> list[dict[str, Any]]:
        now = time.time()
        if self._defs_cache and now - self._defs_at < _DEFS_TTL_SECONDS:
            return self._defs_cache
        try:
            raw = await self.ctx.tool.get_definitions()
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[planner-fixer] 获取工具定义失败: %r", exc)
            return []
        defs = _normalize_definitions(raw)
        self._defs_cache = defs
        self._defs_at = now
        return defs

    async def _resolve_reply_tool(self) -> tuple[str, str] | None:
        """返回 (工具名, 正文参数名)，全部来自真实注册的工具定义。"""
        cfg = self.config.fixer
        defs = await self._tool_definitions()
        if not defs:
            return None
        by_name = {d["name"]: d for d in defs}
        for want in (cfg.reply_tool_names or []):
            tool = by_name.get(str(want).strip())
            if not tool:
                continue
            arg = _pick_text_arg(tool, list(cfg.text_arg_names or []))
            if arg:
                return tool["name"], arg
            self.ctx.logger.info("[planner-fixer] 工具 %s 没有可填写的文本参数，跳过", tool["name"])
            return None
        return None

    @HookHandler(
        HOOK_NAME,
        name="planner_reply_fixer",
        description="把 planner 的纯文字『想回复』意图改写为已注册回复工具的调用",
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

            # 1) 先看否定信号：模型明确说静默/不调用 → 绝不动手
            hit_silence = [str(k) for k in (cfg.silence_keywords or []) if str(k).lower() in lowered]
            if hit_silence:
                self.ctx.logger.debug("[planner-fixer] 命中静默信号 %s，跳过", hit_silence)
                return None

            # 2) 必须是明确的「调用 reply 工具」强信号
            hit_reply = [str(k) for k in (cfg.reply_keywords or []) if str(k).lower() in lowered]
            if not hit_reply:
                return None

            if not await self._model_in_scope():
                return None

            resolved = await self._resolve_reply_tool()
            if not resolved:
                self.ctx.logger.info("[planner-fixer] 没有可用的已注册回复工具，放弃兜底")
                return None
            tool_name, arg_name = resolved

            # 3) 只认真正提取到的台词；没提取到默认不乱发
            extracted = _extract_reply(text, int(cfg.max_reply_chars), "")
            if not extracted:
                if not cfg.allow_fallback:
                    self.ctx.logger.info("[planner-fixer] 命中 %s 但没提取到台词，跳过（allow_fallback=false）", hit_reply)
                    return None
                extracted = cfg.fallback_text
            reply_text = extracted
            if not reply_text:
                return None

            if cfg.dry_run:
                self.ctx.logger.info(
                    "[planner-fixer][dry-run] 命中，工具=%s 参数=%s 回复=%s", tool_name, arg_name, reply_text
                )
                return None

            turn_id = ""
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
                    "func_name": tool_name,
                    "args": {arg_name: reply_text},
                    "extra_content": {},
                },
            }

            self.ctx.logger.info(
                "[planner-fixer] 兜底改写为 %s(%s=%s)", tool_name, arg_name, reply_text
            )
            return {"modified_kwargs": {**kwargs, "output_items": [call_item]}}
        except Exception as exc:  # noqa: BLE001
            self.ctx.logger.warning("[planner-fixer] 兜底失败，保留原始输出: %r", exc)
            return None


def create_plugin() -> PlannerReplyFixer:
    """宿主加载入口（MaiBot 要求插件模块导出该工厂函数）。"""
    return PlannerReplyFixer()
