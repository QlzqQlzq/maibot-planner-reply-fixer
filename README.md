# 小模型决策兜底（Planner Reply Fixer）

> 给「决策脑子不够用」的小模型兜个底：它写出了「我决定调用 reply 工具」，却真的只输出一段文字 —— 这个插件把那段文字翻译成一次**真实工具调用**。

- 插件 ID：`github.QlzqQlzq.planner-reply-fixer`
- 版本：`0.1.2`
- 许可：MIT
- 分类：实用工具（utility）
- 适用宿主：MaiBot **1.0.0 及以上**（SDK 2.x 插件系统；0.11.x 旧插件系统不支持）

## 它解决什么问题

小参数量模型（例如 `spark-x2.6-4b`）在 planner 阶段经常这样输出：

```
……因此决策为调用 reply，用简洁自然的口吻回应「测试收到，我在的」。
```

但它**没有真的发起 tool call**，宿主判定「无调用」，于是角色一句话都不说。

本插件监听 `maisaka.planner.after_response`：当输出里**没有任何工具调用**、
且文本命中「决定调用回复工具」的强信号时，把文本里真正的台词提取出来，
包装成一次**平台已注册回复工具**的调用。

## 设计原则（重要）

1. **不伪造工具**：工具名与参数名全部来自 `ctx.tool.get_definitions()`。
   工具不在真实清单里、或该工具没有可填的文本参数 → **直接放弃**，不改写输出。
2. **尊重「不说话」的决定**：命中 `不进行工具调用 / 保持静默 / wait` 等否定信号时绝不插手。
3. **不硬凑内容**：默认提取不到台词就不发（`allow_fallback = false`）。
4. **可观察**：`dry_run = true` 时只写日志，不改写。
5. **失败即放行**：任何异常都吞掉并返回原样，绝不影响宿主原有流程。

## 判定流程

```
输出里有 FunctionCallItem / tool_call ?  → 是：完全不动
                ↓ 否
文本长度 < min_text_chars ?              → 是：不动
                ↓ 否
命中 silence_keywords（静默信号）?        → 是：不动
                ↓ 否
命中 reply_keywords（强信号）?            → 否：不动
                ↓ 是
planner 模型在 models 名单里?             → 否：不动
                ↓ 是
能从 ctx.tool.get_definitions() 找到真实回复工具及其文本参数?  → 找不到：不动
                ↓ 找到
提取到台词？                              → 否且 allow_fallback=false：不动
                ↓ 是
改写成 FunctionCallItem(真实工具名, {文本参数: 台词})
```

## 安装

1. 把本仓库整个目录放到 MaiBot 的 `plugins/` 下（目录名随意，如 `planner_reply_fixer`）。
2. 确认目录根部有 `_manifest.json`、`plugin.py`、`config.toml`。
3. 重启 MaiBot（或在 WebUI 插件页重载）。
4. 日志出现 `[planner-fixer] 已加载，模型名单=[...] 工具名=['reply'] dry_run=False` 即加载成功。

## 配置说明（`config.toml`）

| 键 | 默认值 | 说明 |
|---|---|---|
| `plugin.enabled` | `true` | 插件总开关 |
| `plugin.config_version` | `"0.1.1"` | 配置版本（宿主校验用） |
| `fixer.enabled` | `true` | 兜底开关 |
| `fixer.models` | `["spark-x2.6-4b"]` | 只对这些 planner 模型生效；`[]` = 不限制 |
| `fixer.reply_tool_names` | `["reply"]` | 回复类工具名，**必须**是已注册工具 |
| `fixer.text_arg_names` | `["text","content","message","msg","reply","reply_text"]` | 在该工具自己的 schema 里找正文参数 |
| `fixer.dry_run` | `false` | `true` = 只记日志不改写 |
| `fixer.allow_fallback` | `false` | 提取不到台词时是否用兜底句发送 |
| `fixer.fallback_text` | `"嗯。"` | 兜底句（仅 `allow_fallback=true` 时使用） |
| `fixer.max_reply_chars` | `120` | 兜底回复最大字数 |
| `fixer.min_text_chars` | `20` | 输出短于该长度直接忽略 |
| `fixer.reply_keywords` | 见下 | 「决定调用回复工具」的强信号 |
| `fixer.silence_keywords` | 见下 | 「决定静默」的否定信号（命中即放弃） |

强信号默认：`调用reply`、`调用 reply`、`调用reply工具`、`使用reply`、`使用 reply`、`reply工具`、`reply 工具`

否定信号默认：`不进行工具调用`、`不调用`、`保持静默`、`保持沉默`、`无需回复`、`不需要回复`、`不回复`、`不发言`、`不必回复`、`wait`

## 日志样例

```
[planner-fixer][dry-run] 命中，工具=reply 参数=text 回复=测试收到，我在的
[planner-fixer] 兜底改写为 reply(text=测试收到，我在的)
[planner-fixer] 命中静默信号 ['保持静默']，跳过
[planner-fixer] 没有可用的已注册回复工具，放弃兜底
```

## 兼容性

| 宿主版本 | 结果 |
|---|---|
| 1.0.0 / 1.0.0-pre.18 / 1.0.12 / 1.2.0 / 1.3.10 | 加载通过（各版本自带校验器 + SDK 2.7.1 实测） |
| 0.11.x（0.12.2 一键包等） | 不支持：旧插件系统无 `maibot_sdk` |

## 隐私

- 不联网、不采集、不上传任何内容。
- 只读宿主已注册的工具定义，用于确定工具名与参数名。

## 开发与验证

- 用真实 planner 决策记录（含「该静默」反例）做过回归：静默反例不会误发。
- 用 MaiBot 1.0.0 ~ 1.3.10 各版本自带 `manifest_validator` 校验 manifest 通过。

## License

MIT
