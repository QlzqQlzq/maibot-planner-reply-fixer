# 小模型决策兜底（Planner Reply Fixer）

麦麦（MaiBot）插件。小模型当 planner 的时候，经常把「要调用工具」写成一段文字而不是真的去调，
插件负责把那段文字还原成一次真正的工具调用。

- 插件 ID：`github.QlzqQlzq.planner-reply-fixer`
- 版本：0.1.2
- 许可：MIT
- 宿主：MaiBot 1.0.0 及以上（SDK 2.x 插件系统）

## 背景

用 `spark-x2.6-4b` 这类小模型跑 planner，会看到这样的输出：

```
决策：调用reply工具，让希罗发一条温和的简短回应。
调用reply工具：
- msg_id: 使用当前最新消息 msg_id="-123456"
- expression_intent:
  - tone: 温和、柔和
  - scene: 关怀回应
  - focus: 对方的「摸摸头」请求
  - reply_act: 确认关怀、柔和回应
  - reply_style: 正常回复
```

看起来像调用了，其实只是文字。宿主判定「没有工具调用」，于是角色一句话都不说。

## 它做什么

监听 `maisaka.planner.after_response`，在输出里没有任何工具调用时：

1. 从文字里解析 `- key: value` 结构（支持一层嵌套，比如 `expression_intent` 下面那几个）；
2. 拿平台已注册工具的 `parameters_schema` 去过滤、收敛类型和枚举、补齐必填项；
3. 拼成真正的工具调用丢回去，后面的台词由 replayer 自己生成。

上面那段会变成：

```json
{
  "msg_id": "-123456",
  "reply_style": "正常回复",
  "expression_intent": {
    "tone": "温和、柔和",
    "scene": "关怀回应",
    "focus": "对方的「摸摸头」请求",
    "reply_act": "确认关怀、柔和回应"
  },
  "reply_reference": "focus: 对方的「摸摸头」请求；reply_act: 确认关怀、柔和回应；tone: 温和、柔和；scene: 关怀回应"
}
```

注意 `reply_reference` 是给 replayer 看的上下文说明，**不是台词**。麦麦的 `reply` 工具没有台词参数，
所以插件不会往里面塞任何「回复内容」。

## 它不会做什么

- 不会自己造工具名。工具名和参数名都从 `ctx.tool.get_definitions()` 里取，找不到就什么都不干。
- 不会编台词。除非那个工具真的声明了文本参数，否则一律不猜。
- 不会打断「不说话」的决定。文字里出现「不进行工具调用」「保持静默」「wait」这类，直接跳过。
- 不会影响宿主。任何异常都吞掉、返回原样，出问题最多是没帮上忙。

## 判定流程

```
已经有 FunctionCallItem / tool_call   → 不动
文字太短（< min_text_chars）          → 不动
命中静默关键词                        → 不动
当前 planner 模型不在 models 名单里    → 不动
解析不出「调用X工具：- key: value」     → 不动
找不到对应的已注册工具                → 不动
必填参数补不齐                        → 不动
否则 → 改写成真实工具调用
```

## 安装

1. 把整个目录丢到 MaiBot 的 `plugins/` 下，目录名随意，比如 `planner_reply_fixer`；
2. 确认根目录有 `_manifest.json`、`plugin.py`、`config.toml`；
3. 重启 MaiBot，或到 WebUI 插件页重载；
4. 日志里能看到这行就算装好了：

```
[planner-fixer] 已加载，模型名单=['spark-x2.6-4b'] 工具名=['reply'] spec解析=True dry_run=False
```

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `plugin.enabled` | `true` | 插件总开关 |
| `plugin.config_version` | `"0.1.2"` | 配置版本，宿主校验用 |
| `fixer.enabled` | `true` | 兜底开关 |
| `fixer.models` | `["spark-x2.6-4b"]` | 只对这些 planner 模型生效；留空 `[]` 表示不限制 |
| `fixer.tool_names` | `["reply"]` | 允许兜底的工具，必须已注册 |
| `fixer.enabled_spec_parsing` | `true` | 是否解析「调用X工具：- key: value」这种说明 |
| `fixer.synth_reply_reference` | `true` | 缺 `reply_reference` 时，用 focus / reply_act / tone / scene 拼一句补上 |
| `fixer.text_arg_names` | `["text","content","message","msg","reply","reply_text"]` | 只有当工具真的声明了这些参数时才会用到 |
| `fixer.dry_run` | `false` | 设成 `true` 只写日志不改写，适合先观察 |
| `fixer.min_text_chars` | `20` | 输出短于这个长度直接忽略 |
| `fixer.max_reply_chars` | `120` | 从引号里取台词时的长度上限 |
| `fixer.silence_keywords` | 见下 | 命中就当模型决定不说话，直接跳过 |

静默关键词默认：`不进行工具调用`、`不调用`、`保持静默`、`保持沉默`、`无需回复`、`不需要回复`、`不回复`、`不发言`、`不必回复`、`wait`

## 日志

```
[planner-fixer][dry-run] 命中 tool=reply args={'msg_id': '-123456', ...}
[planner-fixer] 兜底还原为 reply({'msg_id': '-123456', ...})
[planner-fixer] 命中静默信号 ['保持静默']，跳过
[planner-fixer] 未发现工具调用说明，跳过
[planner-fixer] 缺少必填参数 ['reply_reference']，放弃兜底（tool=reply）
```

## 已知限制

- 如果模型只写「因此调用 reply，回应『xxx』」这种纯散文、没有 `- key: value` 结构，插件拿不到 `msg_id`，
  就不会动手。宁可不动，也不乱猜。
- 只在 SDK 2.x 的宿主上工作。MaiBot 0.11.x（0.12.2 一键包那种）是旧插件系统，没有 `maibot_sdk`，装不了。

## 测试情况

- 用真实的 planner 输出跑过回归，包括「伪调用说明」「纯散文」「明确表示静默」三种；
- 用 MaiBot 1.0.0、1.0.0-pre.18、1.0.12、1.2.0、1.3.10 各自自带的 `manifest_validator` 校验 manifest，
  都能通过。

## License

MIT
