# 小模型决策兜底

麦麦（MaiBot）插件。用小模型当 planner 的时候，它经常「嘴上说要调用工具，实际上什么都没调」，
结果 bot 该回的话一句都不说。装上这个插件，遇到这种情况会自动把这次调用补上。

- 插件 ID：`github.QlzqQlzq.planner-reply-fixer`
- 版本：0.1.13
- 许可：MIT

## 装上之后

小模型输出「调用reply工具：- msg_id: … - expression_intent: …」这类带参数的文字时，
插件会按工具自己的参数表把它还原成一次真正的 reply 调用，bot 该回的话照常回。
回复内容由麦麦自己生成，插件不写台词。

模型这轮明确表示不说话（比如「保持静默」「不进行工具调用」「stay silent」）时，插件不会插手。

## 安装

1. 把整个文件夹放进 MaiBot 的 `plugins` 目录，文件夹名字随意，比如 `planner_reply_fixer`；
2. 文件夹里要有 `_manifest.json`、`plugin.py`；
3. 重启 MaiBot，或者在 WebUI 的插件页重载一下；
4. 日志里出现这行就说明装好了：

```
[planner-fixer] 已加载，模型名单=不限制 工具白名单=['reply'] spec解析=True dry_run=False
```

## 配置

第一次加载后，麦麦会按插件自带的配置结构自动生成 `config.toml`，改它就行（仓库里不带这个文件，免得更新时冲突）。

| 键 | 默认值 | 说明 |
|---|---|---|
| `fixer.enabled` | `true` | 插件开关 |
| `fixer.models` | `[]` | 只对这些 planner 模型生效；留空表示所有模型都管 |
| `fixer.tool_names` | `["reply"]` | 白名单。只有这里的工具才会被补上，其它工具一律不碰 |
| `fixer.enabled_spec_parsing` | `true` | 是否解析「调用X工具：- key: value」这种说明；关掉插件就基本不工作了 |
| `fixer.synth_reply_reference` | `true` | 缺 `reply_reference` 时，用 focus / reply_act / tone / scene 拼一句补上 |
| `fixer.prose_mode` | `true` | 模型只写「可以简短接一句」这类散文时，插件自己从上下文取消息 id 补一次调用 |
| `fixer.prose_tool_name` | `reply` | 散文模式使用的工具（必须在 `tool_names` 里） |
| `fixer.dry_run` | `false` | 设成 `true` 只记日志、不真的发，适合先看看效果 |
| `fixer.min_text_chars` | `10` | 输出短于这个长度直接忽略 |
| `fixer.silence_keywords` | `[]` | 额外静默词。内置静默判断（保持静默 / 不要调用 / stay silent 等）始终生效，这里只是补充 |

只想让自己用的小模型生效，就这样写：

```toml
[fixer]
models = ["spark-x2.6-4b"]
```

## 什么情况下会动手

同时满足才会补，满足不了就什么都不做：

- 这一轮麦麦把工具列表和上下文交给了插件（after_response 只有输出，上下文必须在 before_request 时缓存）；
- 回复里没有出现真正的工具调用；
- 文本里能解析出 `- key: value` 的参数结构；
- 点名的工具在 `tool_names` 白名单里，且这一轮确实存在；
- 必填参数能补齐（`reply_reference` 缺了会放弃）；
- 模型没有表示要静默。

## 日志

```
[planner-fixer] 本轮工具 5 个：['wait', 'reply', 'query_memory', ...]
[planner-fixer] 兜底还原为 reply({'msg_id': '-123456', ...})      补上了一次调用
[planner-fixer][dry-run] 命中 tool=reply args={...}                只在 dry_run 下出现，没真的发
[planner-fixer] 命中内置静默规则，跳过                              模型不想说话，插件没插手
[planner-fixer] 点名的工具 send_group_msg 不在白名单 ['reply'] 内   点名的不是白名单工具，忽略
[planner-fixer] 缺 reply_reference，放弃兜底（tool=reply）          参数不全，不乱发
```

## 兼容性

MaiBot 1.0.0 及以上。

MaiBot 0.11.x（0.12.2 一键包那种）装不了，那是旧插件系统。

## License

MIT
