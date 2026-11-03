"""Memory Beyond 核心逻辑：不依赖 AstrBot，本包内全部可独立测试。

    tokens.py    —— token 估算（口径对齐 AstrBot 平台的字符启发式）
    turns.py     —— 消息轮次切分（tool_call 与其结果不可分割）
    memstore.py  —— 文件式记忆库：作用域、索引、截断、写入守门
    history.py   —— 自动历史、回收站与审计
    insights.py  —— 小节替换、搜索排序与片段、悬空链接与长文件提醒
    compress.py  —— 摘要 + 水位线的上下文压缩状态机
    prompts.py   —— 注入块、system prompt 规范、摘要提示词模板
    injection.py —— 注入视图：去 frontmatter、头尾瘦身、标题与档案区
"""
