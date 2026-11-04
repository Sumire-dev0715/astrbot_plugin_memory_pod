# 鲸群记忆

AstrBot 的长期记忆插件，自带上下文压缩。记忆存成本地的 Markdown 文件，不需要 Embedding 模型。插件标识是 `astrbot_plugin_memory_pod`。

基于 [AlanBacker/astrbot_plugin_memory_beyond](https://github.com/AlanBacker/astrbot_plugin_memory_beyond) v1.4.1 修改，原项目采用 MIT 许可证。

## 它做什么

- **记住人和事。** 每个群聊、私聊各有一份记忆。一个人是谁（身份、职业、常用称呼、长期偏好）另外单独存一份，他换个群、改成私聊，机器人也认得。
- **后台整理记忆。** 聊天模型只读记忆，不用分心去写。对话停下 10 分钟或每聊 10 轮，后台模型把这段对话整理进记忆，并记一条日志。用户说"记住""忘掉"时，聊天模型把要求交给后台，这一轮结束就处理。
- **只带用得上的。** 每轮请求开头带一份记忆目录，新消息前附上最相关的几条记忆。私聊时对方的人物档案放在开头，不用每轮重发。
- **自带上下文压缩。** 上下文快满时把较早的对话压成摘要。窗口大小跟着 AstrBot 的设置走，不用改 AstrBot 的配置。
- **改了能恢复。** 覆盖、修改、删除前自动保存旧版，删掉的文件进回收站，在管理页里查看和恢复。
- **多个机器人共享人物记忆**（可选）。同一个 AstrBot 里的几个 QQ 号可以共用人物记忆，每个机器人有自己的写权限。

## 安装

在 AstrBot WebUI 里用本仓库地址安装，或者把仓库克隆到 `data/plugins/astrbot_plugin_memory_pod`。插件只用 Python 标准库和 AstrBot 的插件接口，没有第三方依赖。开发和测试用的是 AstrBot 4.29.4，记忆管理页需要 4.24.1 及以上。

装好就能用，不需要改 AstrBot 的设置。建议在插件配置里指定两个模型：

- `summary_provider_id`：压缩摘要用，选便宜、快的；
- `memory_provider_id`：后台整理记忆用。这一步要读懂对话、给出结构化的修改，建议选能力强一些的。

两项都留空时用当前对话的模型。

AstrBot 的「用户识别」开着的话，发送者会被标注两次（AstrBot 一次，插件一次）。这不影响使用，嫌多余可以关掉 AstrBot 那一项。

## 从 Memory Beyond 迁移

插件标识换了，AstrBot 会用新的配置文件和数据目录。先停用 Memory Beyond，再复制旧数据：

```bash
mkdir -p data/plugin_data/astrbot_plugin_memory_pod
cp -a data/plugin_data/astrbot_plugin_memory_beyond/. data/plugin_data/astrbot_plugin_memory_pod/
cp -a data/config/astrbot_plugin_memory_beyond_config.json data/config/astrbot_plugin_memory_pod_config.json
```

两个插件不要同时启用。另外：

- 旧的记忆文件原样能用，`daily-*.md` 会被当作日志。
- 这几个配置项已经去掉，留在配置文件里也不生效：`annotate_sender`、`enable_people_memory`、`pin_private_profile`（这三项现在恒开），以及 `extract_memories`、`archive_keep`。`consolidate_provider_id` 改名为 `memory_provider_id`，旧名字仍然认。
- 如果按 Memory Beyond 的说明把 AstrBot 的「上下文窗口兜底值」改成了超大数值，请改回默认，或者在插件里填上 `max_context_tokens`，否则插件可能拿不到可信的窗口大小而停用压缩。「压缩前最多保留对话轮数」设成了 -1 的可以不动。
- 压缩归档功能已经去掉，数据目录里的 `archive/` 不再使用，可以删除。

## 记忆怎么存

记忆分三层作用域：

| 作用域 | 范围 | 存什么 |
| --- | --- | --- |
| `session` | 一个群聊或一个私聊 | 这里的人和事：和机器人的关系、群内角色、健康和家庭这类私密的事、进行中的事项、对机器人的要求 |
| `people` | 一个平台的所有会话 | 某个人亲口说的、换个群也成立的身份类事实，一人一个 `user-<数字ID>.md` |
| `global` | 所有会话 | 机器人自己的行为准则；默认关闭 |

每个文件记一件事，类型是 `user`（人）、`feedback`（对机器人的要求）、`project`（进行中的事）、`reference`（资料）、`journal`（日志）之一。文件开头的 frontmatter 由插件生成：`description` 是一句话摘要，`created`、`updated` 是首次写入和最近修改的时间，`source` 是说出这条信息的人的 ID。每个作用域有一个 `MEMORY.md` 索引，一个文件占一行，由插件维护。

数据目录是 `data/plugin_data/astrbot_plugin_memory_pod/`：

```text
memories/
  session/<会话>/      每个会话的记忆
  people/<平台>/       人物记忆；共享人物空间是 people/_shared/
  global/              全局记忆
  .trash-scopes/       整个会话的记忆被清除时，目录先挪到这里
state/                 压缩状态；state/pending/ 是待整理的对话
tidy/                  定期整理的状态和整理记录
sessions.json          会话名称和备注
```

每个作用域目录里除了记忆文件，还有 `MEMORY.md`、`.usage.json`（使用统计）、`forgotten/`（已遗忘）、`.history/`（历史版本）、`.trash/`（回收站）和 `.audit.jsonl`（审计）。

## 记忆怎么写

记忆只由后台写入：

1. 每轮对话结束，插件把用户的消息（带发送者标注）和机器人的回复记进这个会话的待整理记录。工具调用不记。
2. 会话空闲 `extract_idle_minutes`（默认 10）分钟后、每聊 `auto_extract_interval`（默认 10）轮、或者聊天模型调用了 `memory_note`，插件把待整理的对话交给记忆整理模型。所有整理排在一个队列里，一次只跑一个。
3. 模型看到这段对话、相关的已有记忆和规则，给出一组修改（新建、追加、按小节替换、替换一句、删除）和一条日志。插件逐条检查文件名、作用域、类型、长度和权限，合格的才写入，写入前照常保存旧版。
4. 写入成功后删掉这段待整理记录。失败的下次再试；同一段连续失败 3 次就放弃，并在日志里告警。

后台模型遵守这些规则：

- 人物记忆用中性的第三人称，只记本人亲口说的身份类事实，私密的事记在会话里；
- 全局记忆只在 AstrBot 管理员提出、而且内容是对机器人自身的要求时才写；
- 删除或更正某个人的记忆，只在本人或管理员要求时执行，别人说的只记成转述；
- 群里有人要求机器人改变对别人的做法，只记成这个人的意见；
- 对话内容是数据，"记住：以后你要……"这类话也按上面的规则判断，不会变成指令。

**日志。** 每段对话的流水记进日志，默认接着最近一篇写。最近一篇超过 3000 字、或者最后一次更新已经过了 7 天，就新开一篇 `journal-<日期>.md`，所以几天才聊一次的人不会每次都多出一篇。日志不参与召回和查重，在索引里折叠成"最近两篇加一行汇总"。

**整理。** 管理页可以让模型复盘一处或全部记忆，合并重复、修正过时的内容，你确认后才写入。也可以开启 `auto_consolidate` 定期整理。

## 记忆怎么用

聊天模型有三个工具：

| 工具 | 作用 |
| --- | --- |
| `memory_read(scope, file)` | 读一个记忆文件的全文；`file` 省略时读该作用域的索引。读到已遗忘的文件会把它恢复 |
| `memory_search(query, scope, mode)` | 全文搜索，`mode=any` 时命中任意一个词即可；最多返回 8 条，日志排在后面，每条带命中片段 |
| `memory_note(content)` | 用户明确要求记住、更正或忘掉某件事时，用一句话记下要求，交给后台处理 |

插件还会在每轮请求里自动带上两块内容：

- **开头块**：会话和人物记忆的索引，私聊时还有对方的人物档案。它是一份快照，只在压缩、换对话、重置或者在管理页改了记忆时刷新，所以能命中模型服务的提示词缓存。刷新之前新写入的记忆不会出现在索引里，和当前消息相关时由召回带上。
- **召回块**：放在新消息之前，按关键词（或者用摘要模型）挑出最多 `recall_max_files`（默认 5）个相关文件。发送者和被 @ 的人的档案带全文；其余文件里相关度最高的 `recall_full_files`（默认 2）个带全文，剩下的只列文件名。单个文件超过 3000 字时保留开头和结尾，整块合计不超过 9000 字。

模型会被告知，索引和召回里的内容只是数据，看起来像指令的文字一律不执行。

## 上下文压缩

- **窗口跟着 AstrBot 走。** `max_context_tokens` 为 0 时，插件按 AstrBot 自己的顺序取窗口大小：提供商里填的窗口、AstrBot 自带的模型表、AstrBot 的「上下文窗口兜底值」。上下文超过窗口的 `compress_threshold`（默认 0.70）就压缩，抢在 AstrBot 自己的压缩（窗口的 82%）之前。AstrBot 设了「压缩前最多保留对话轮数」时，快到上限也会先压缩。
- **滚动摘要。** 较早的对话压成一段摘要，最近 `keep_recent_turns`（默认 3）轮保留原文。摘要模型失败时先重试，再失败就退避一段时间，期间用裁剪过的上下文顶上。
- **工具循环保护**（`loop_guard`，默认开）：一次回复里连续调用很多工具时，每个工具返回后估算下一次请求的大小，快超过时把较早的工具结果换成一句占位说明。
- **空闲清理**（`tool_result_idle_minutes`，默认 60 分钟）：会话空闲这么久之后，提示词缓存已经失效，插件把较早的长工具结果换成占位说明。

关掉 `enable_compression` 后，插件完全不改动上下文，由 AstrBot 自带的压缩负责。

压缩不影响聊天软件里的记录。AstrBot 存的会话历史会变成"摘要加最近几轮"，被摘要替代的原文不再保留。

## 多个机器人共享人物记忆

在 `shared_people_bots` 里列出要共享的机器人。每项写 `platform_id` 或 `platform_id=edit|append|read`，不写等级时是 `append`：

- `edit`：可以新建、修改、删除共享的人物记忆；
- `append`：只能新建和追加；
- `read`：只读。

列出的机器人共用 `people/_shared/`，没列的仍用自己的人物目录。共享的只有人物记忆，每个会话的记忆不共享。成员必须是同一种平台（比如都是 QQ），否则数字 ID 可能撞号；类型不一致的成员会被关掉共享，并在日志里告警。

已有的人物记忆可以用迁移脚本复制进共享空间。脚本默认只预览，加 `--apply` 才写入；同一个人在两边都有文件时不覆盖，而是列进冲突报告；原目录不删。

```bash
python scripts/migrate_people_to_shared.py --data-dir data/plugin_data/astrbot_plugin_memory_pod --from Bot-A --from Bot-B
```

## 历史、回收站与遗忘

- 每次覆盖、修改、删除前，旧内容存进该作用域的 `.history/`，删掉的文件进 `.trash/`。每次变更在 `.audit.jsonl` 记一行：时间、动作、文件名、谁改的、内容哈希，不含正文。
- 历史版本保留 `history_keep_days`（默认 90）天，每个文件至少留最近 10 版；回收站保留 `trash_keep_days`（默认 90）天。填 0 表示永久保留。
- 管理页里可以查看、恢复任何一版。有人要求删除自己的信息时，用「彻底删除（含历史）」把文件、历史版本和回收站里的副本一起删掉；审计里那几行只有文件名、时间和哈希，会保留下来。
- 遗忘（`enable_forgetting`，默认关）：连续 `forget_after_days`（默认 90）天没被召回或读取的记忆移进 `forgotten/`，不再进索引和召回。这个人再出现、模型去读它、或者在管理页点恢复，它就回来。`feedback` 类不参与遗忘。

## 记忆管理页

在 AstrBot WebUI 侧边栏的「插件页面」里，可以：

- 按会话查看、搜索、新建、编辑、改名、删除记忆，文件列表会标出过长的文件和指向不存在文件的 `[[链接]]`；
- 查看每个文件的历史版本，以及每个作用域的回收站和已遗忘；
- 查看索引的占用、没登记的文件和失效的行，手工调整或一键重建；
- 整理一处或全部记忆，设置定期整理，查看并撤销最近 5 次整理；
- 导出和导入备份，机器人换了平台 ID 后整体搬迁记忆，或者把记忆迁到另一个会话；
- 查看上下文压缩的水位线、当前摘要、各部分占比和提示词缓存命中；
- 查看待整理的对话条数、上次整理的结果和连续失败次数；
- 查看共享人物记忆的成员和各自的权限。

页面里的改动从下一轮对话起生效。

## 配置项

| 配置 | 默认 | 说明 |
| --- | --- | --- |
| `enable_memory` | 开 | 总开关；关掉后不注入记忆、不整理，已有文件保留 |
| `enable_global_memory` | 关 | 全局记忆（机器人自己的行为准则） |
| `recall_mode` | `keyword` | 召回方式：`keyword` 按关键词，`llm` 让摘要模型挑，`off` 关闭 |
| `recall_max_files` | 5 | 每轮最多召回几个文件 |
| `recall_full_files` | 2 | 其中带全文的个数，身份档案不占名额 |
| `enable_compression` | 开 | 上下文压缩 |
| `max_context_tokens` | 0 | 模型的上下文窗口；0 表示跟随 AstrBot |
| `compress_threshold` | 0.70 | 上下文占窗口多少时压缩，必须小于 0.82 |
| `compress_buffer` | 0.165 | 压缩后的占用比触发线再低多少 |
| `keep_recent_turns` | 3 | 压缩时保留原文的轮数 |
| `summary_provider_id` | 空 | 摘要模型；空则用当前对话的模型 |
| `memory_provider_id` | 空 | 记忆整理模型；空则用摘要模型 |
| `auto_consolidate` | 关 | 定期整理：关闭、整理前问我、直接整理 |
| `auto_consolidate_hours` | 24 | 两次定期整理的最短间隔（小时） |
| `summary_retry_count` | 3 | 摘要失败时最多尝试几次 |
| `summary_timeout` | 120 | 单次摘要的超时（秒） |
| `summary_prompt_template` | 空 | 自定义摘要提示词 |
| `summary_max_chars` | 1500 | 摘要字数上限 |
| `auto_extract_interval` | 10 | 每聊几轮整理一次记忆；0 关闭 |
| `extract_idle_minutes` | 10 | 会话空闲几分钟后整理记忆；0 关闭 |
| `shared_people_bots` | 空 | 共享人物记忆的机器人和权限 |
| `history_keep_days` | 90 | 历史版本保留天数；0 永久 |
| `trash_keep_days` | 90 | 回收站保留天数；0 永久 |
| `enable_forgetting` | 关 | 记忆遗忘 |
| `forget_after_days` | 90 | 多少天没用到就遗忘 |
| `tool_result_idle_minutes` | 60 | 会话空闲多久后清理旧的工具结果（分钟） |
| `loop_guard` | 开 | 工具循环中途的上下文保护 |

## 管理指令

只有 AstrBot 管理员能用：

| 指令 | 作用 |
| --- | --- |
| `/mb_status` | 本会话的记忆和压缩状态：窗口、水位线、上下文各部分的估算、待整理的对话、整理和遗忘的设置、共享权限 |
| `/mb_reset` | 清空本会话的摘要和水位线；已经被压缩掉的原文不会回来 |
| `/mb_probe` | 测试摘要模型的回退链能不能用 |

## 常见问题

**刚聊的事，机器人下次会记得吗？** 同一段对话里，模型本来就看得到上下文。对话停下 10 分钟后会整理进记忆，之后再聊就能召回。急的话可以直接说"记住……"，这一轮结束就会整理。

**群里的记忆会被别的群看到吗？** 会话记忆只在本会话里用。人物记忆在同一平台的所有会话里共享，所以只放本人说的、换个群也成立的身份类事实，私密的事记在会话里。

**有人改了群名片冒充别人怎么办？** 插件在每条消息开头标注发送者的群名片、QQ 名和 QQ 号，记人以 QQ 号为准。有人在正文里手打 `[发送者：…]` 也没用，插件会把它改成全角括号。

**日志里说压缩已停用？** 插件拿不到可信的窗口大小。在插件配置里填上 `max_context_tokens`，或者把 AstrBot 的「上下文窗口兜底值」改回正常的数值。

## 开发

```bash
python -m pytest -q                       # 只需要 pytest，不需要装 AstrBot
python scripts/prompt_sizes.py --json     # 各段提示词的字数
python scripts/eval/run_eval.py --env-file ~/.config/memory-pod/eval.env --runs 3 --json
```

评测脚本用 25 段合成对话，通过插件的真实代码调用真实模型，检查记忆有没有写对、该保密的有没有泄露。它从 `--env-file` 指定的文件里读 `MB_EVAL_BASE_URL`、`MB_EVAL_MODEL`、`MB_EVAL_API_KEY`（OpenAI 兼容接口），这个文件不要放进仓库。

代码分三块：`core/` 是不依赖 AstrBot 的逻辑，可以单独测试；`main.py` 对接 AstrBot 的钩子、工具和指令；`web_api.py` 和 `pages/memory/` 是管理页，页面是静态文件，改完在 WebUI 里刷新即可。

## License

MIT，见 [LICENSE](LICENSE)。原项目作者 AlanBacker。