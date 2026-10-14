# AI-RTS

用 AI 当参谋和双手，玩家只做指挥官的 RTS。玩家用自然语言下任何抽象层级的指令，
从开局 brief 大战略到"空投金甲偷矿"，AI 负责执行并汇报。

当前阶段：技能库接口 + 极简沙盒 + 最简执行器，三个技能能在沙盒里跑完整生命周期。没有真游戏适配器，没有 LLM 接入。

## 分层

```
战略层   整局计划，阶段 + 触发条件 + 预案            LLM 维护            （未做）
作战层   常驻管理器：经济 / 生产 / 科技 / 防御          脚本，参数由 LLM 设  （未做）
战术层   一次性任务：开矿 / 侦查 / 空投                 技能状态机          <- 本包 skill.py, skills/
微操层   帧级单位控制：走位 / 集火 / 拉农民             脚本或小模型        （未做）
游戏 API move / attack / build / 视野查询               引擎适配器          <- world.py 定义协议

横向两层：
理解层   自然语言 -> 技能调用 / 管理器参数 / 计划修改    LLM + grounding     <- registry.py 导出 tool
反馈层   事件 -> 参谋式汇报，带注意力管理               LLM                 <- events.py 定义输入
```

LLM 只住在战略层和两个横向层，不进战术层和微操层。技能是状态机，可靠、便宜、毫秒级。

## 文件

| 文件 | 内容 |
|---|---|
| `ai_rts/types.py` | 语义类型：Location（区域名 / 钟点 / 坐标）、UnitSelector、角色名 |
| `ai_rts/conditions.py` | 声明式撤退条件和 AbortPolicy，LLM 可直接输出 |
| `ai_rts/world.py` | WorldView（技能看什么）和 Commander（技能能做什么）协议 |
| `ai_rts/arbiter.py` | 单位租约、资金预留、优先级 |
| `ai_rts/events.py` | 技能发给反馈层的结构化事件，severity 是"该不该打断玩家" |
| `ai_rts/skill.py` | Skill 基类、生命周期、SkillContext、执行器契约 |
| `ai_rts/schema.py` | dataclass 参数和 JSON schema 互转，不依赖第三方库 |
| `ai_rts/registry.py` | 注册、别名、导出 LLM tool、从 JSON 实例化 |
| `ai_rts/skills/` | 三个示例技能：scout、expand、drop_harass |
| `examples/print_tool_specs.py` | 导出 tool 列表，演示参数解析 |
| `ai_rts/runner.py` | SimpleArbiter（租约、抢单位、资金排队）、SimpleRunner（驱动生命周期、统计击杀阵亡）、EventLog |
| `ai_rts/sandbox/` | 确定性 2D 模拟器：移动、攻击、采矿、建造、装卸、视野、警报、死亡归因。实现 WorldView 和 Commander |
| `ai_rts/sandbox/scenario.py` | 标准场景：我方人族左上，敌方神族右下，128x128 |
| `tests/test_lifecycle.py` | 九个测试：三个技能各自跑完、三个并发、取消时先接人、高优先级抢单位、前置检查失败、租约越权 |
| `examples/run_sandbox.py` | 同时下三条指令，打印事件流 |

## 一条指令怎么流过去

```
"空投金甲偷矿"
  -> 理解层 LLM 看 registry.tool_specs()，选 drop_harass，填参数 JSON
  -> registry.instantiate() 得到技能实例
  -> skill.check(world) 不通过就把原因念给玩家："没有运输机"
  -> skill.estimate(world) 给玩家一句预估："大约 40 秒到，目标附近有防空"
  -> runner.launch(skill, Priority.DIRECT)
       -> arbiter.request(skill.requirements()) 拿租约
       -> skill.on_start(ctx)，然后每 tick_every 帧 skill.tick(ctx)
       -> 技能通过 ctx.emit 发事件，反馈层决定说不说
       -> 终态时 skill.on_finish 给一句总结："空投 enemy_mineral_line：击杀 7，损失 0，部队已撤回"
```

## 写一个新技能

1. 定义 frozen dataclass 参数，每个字段用 `types.param("说明", 默认值)`。位置用 `Location`，撤退用 `AbortPolicy`。
2. 继承 `Skill[Params]`，填 `name` / `description` / `aliases` / `Params`。
3. 实现 `requirements`、`on_start`、`tick`。按需覆盖 `check`、`estimate`、`on_cancel`、`summarize`。
4. 在 `skills/__init__.py` 注册。

规则：技能只通过 `ctx` 看世界和下命令；只下"打谁、去哪"级别的命令，走位归微操层；
每个阶段切换都 `set_phase`，反馈层靠它回答"空投到哪了"。

## 运行

需要 Python 3.10 以上，零第三方依赖。

```bash
python3 -m unittest -v tests.test_lifecycle
```

```bash
python3 examples/run_sandbox.py
```

```bash
python3 examples/print_tool_specs.py
```

沙盒事件流长这样，这就是反馈层未来的输入：

```
[    1    0.0s] drop_harass#3  INFO     phase     装载 2 个单位，目标 enemy_mineral_line
[ 1289   53.7s] drop_harass#3  INFO     phase     到达目标，卸货
[ 1297   54.0s] drop_harass#3  NOTICE   phase     部队落地，开始骚扰
[ 1357   56.5s] drop_harass#3  INFO     kill      击杀 PROBE
[ 1361   56.7s] drop_harass#3  NOTICE   phase     触发撤退条件，接走部队
[ 1985   82.7s] drop_harass#3  NOTICE   done      空投 enemy_mineral_line：击杀 1，损失 0，部队已撤回
```

## 接真游戏

技能只依赖 `WorldView` 和 `Commander` 两个协议，换游戏就是换适配器。

- 星际 2：官方 API 在 Mac 客户端里就有，`burnysc2` 直接可用，Apple Silicon 走 Rosetta 2。这是最快能上真游戏的路。
- 星际 1：BWAPI 只有 Windows。Mac 上可行的是 OpenBW（引擎重实现，原生编译）加 1.16.1 的 MPQ 文件，或者 Windows 机器跑 BWAPI 再用网络桥接。放在星际 2 验证之后。
