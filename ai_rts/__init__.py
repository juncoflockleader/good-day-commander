"""AI-RTS 技能库接口。

分层对应关系：

- 游戏 API 层   -> world.py      WorldView / Commander 协议，由游戏适配器实现
- 微操层        -> 不在本包       技能只下"打谁、去哪"级别的命令，走位集火由微操层接管
- 战术层        -> skill.py      Skill 生命周期；skills/ 下是具体技能
- 仲裁          -> arbiter.py    单位租约、资金预留、优先级
- 反馈层的输入  -> events.py     技能发出的结构化事件
- 理解层的输入  -> registry.py   技能注册、别名、JSON schema 导出成 LLM tool
"""
