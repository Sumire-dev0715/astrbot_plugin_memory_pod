"""鲸群记忆核心逻辑：不依赖 AstrBot，本包内全部可独立测试。

    tokens.py        —— token 估算（口径对齐 AstrBot 平台的字符启发式）
    context_stats.py —— 上下文统计：估算分项、提供商用量与 AstrBot 摘要
    turns.py         —— 消息内部记录判定与 AstrBot 口径的对话轮数
    reminder.py      —— 写记忆提醒：每 N 轮与截断/压缩前的判断和文本
    memstore.py      —— 文件式记忆库：作用域、索引、截断、写入守门
    history.py       —— 自动历史、回收站与审计
    insights.py      —— 小节替换、搜索排序与片段、悬空链接与长文件提醒
    prompts.py       —— 注入块、system prompt 规范
    injection.py     —— 注入视图：去 frontmatter、头尾瘦身、标题与档案区
    writer.py        —— 写入层：三层作用域的强制规则、操作校验与落盘
"""
