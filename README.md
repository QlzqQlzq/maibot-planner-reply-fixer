# 小模型决策兜底

麦麦（MaiBot）插件。用小模型当 planner 的时候，它经常「嘴上说要调用工具，实际上什么都没调」，
结果 bot 该回的话一句都不说。装上这个插件，遇到这种情况会自动把这次调用补上。

- 插件 ID：`github.QlzqQlzq.planner-reply-fixer`
- 版本：0.1.3
- 许可：MIT

## 装上之后

小模型输出「调用reply工具：- msg_id: … - expression_intent: …」这类文字时，插件会把它整理成一次
真正的 reply 调用，bot 该回的话照常回。台词由麦麦自己生成，插件不写台词。

如果模型这轮明确表示不说话（比如「保持静默」「不进行工具调用」），插件不会去打扰它。

## 安装

1. 把整个文件夹放进 MaiBot 的 `plugins` 目录，文件夹名字随意，比如 `planner_reply_fixer`；
2. 文件夹里要有 `_manifest.json`、`plugin.py`、`config.toml`；
3. 重启 MaiBot，或者在 WebUI 的插件页重载一下；
4. 日志里出现这行就说明装好了：

```
[planner-fixer] 已加载，模型名单=['spark-x2.6-4b'] 工具名=['reply'] spec解析=True dry_run=False
```

## 配置

打开 `config.toml` 改，常用的就下面几个，其它一般不用动。

| 键 | 默认值 | 说明 |
|---|---|---|
| `fixer.enabled` | `true` | 插件开关 |
| `fixer.models` | `["spark-x2.6-4b"]` | 只对这些 planner 模型生效；留空 `[]` 表示所有模型都管 |
| `fixer.tool_names` | `["reply"]` | 允许补的工具，必须是已经装好的工具 |
| `fixer.dry_run` | `false` | 设成 `true` 只记日志、不真的发，适合先看看效果 |
| `fixer.silence_keywords` | 见下 | 文字里出现这些词就当作模型不想说话，插件不动 |

静默关键词默认：`不进行工具调用`、`不调用`、`保持静默`、`保持沉默`、`无需回复`、`不需要回复`、`不回复`、`不发言`、`不必回复`、`wait`

## 日志

```
[planner-fixer] 兜底还原为 reply({'msg_id': '-123456', ...})   补上了一次调用
[planner-fixer][dry-run] 命中 tool=reply args={...}             只在 dry_run 下出现，没真的发
[planner-fixer] 命中静默信号 ['保持静默']，跳过                   模型不想说话，插件没插手
[planner-fixer] 未发现工具调用说明，跳过                          这轮不需要处理
```

## 兼容性

MaiBot 1.0.0 及以上。

MaiBot 0.11.x（0.12.2 一键包那种）装不了，那是旧插件系统。

## License

MIT
