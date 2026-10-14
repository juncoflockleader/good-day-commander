# AI-RTS

用 AI 当参谋和双手，玩家只做指挥官的 RTS。玩家用自然语言下任何抽象层级的指令，
从开局 brief 大战略到"空投金甲偷矿"，AI 负责执行并汇报。

当前阶段：技能库接口 + 沙盒 + 执行器 + 星际 2 适配器 + 理解层。一句话经 LLM 变成技能调用，在沙盒和真实星际 2 客户端里都跑通了。

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
| `ai_rts/adapters/sc2.py` | 星际 2 适配器：WorldView / Commander 落到 python-sc2，地图语义开局算一次，击杀归因靠推断 |
| `examples/sc2_run.py` | 在真游戏里下指令，可用调试接口变单位，可存 replay |
| `docker/` | 无头 Linux 客户端镜像、运行脚本、Rosetta 用的 mutex 对齐垫片 |
| `ai_rts/understanding.py` | 理解层：系统提示 + 词表 + 工具调用 → 注册表校验 → 一轮修复 → 可行性检查 |
| `ai_rts/llm.py` | LLM 后端抽象：DeepSeek（OpenAI 兼容接口，零依赖）和 Claude（Anthropic SDK） |
| `ai_rts/vocab.py` | 黑话词表：枪兵 → MARINE，高地下 → my_natural，偷矿 → drop_harass |
| `examples/interpret.py` | 一句话 → 指令，可直接在沙盒里执行 |
| `docker/say.sh` | 一句话直接进真游戏 |
| `tests/test_understanding.py` | 假 LLM 测解析、修复轮、可行性检查，不联网 |

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

## 理解层：一句话变指令

理解层只做翻译，不碰执行。系统提示里是参谋的角色、六条规则和黑话词表；工具列表就是注册表导出的技能 schema。
LLM 返回的每个 tool call 先经注册表实例化校验，不合法的拿错误信息再问一轮；给了 world 的话还会跑技能的 `check()`，
把"没有运输机"这类可行性问题一起报出来。

凭证：DeepSeek 用 `DEEPSEEK_API_KEY`，或 `DEEPSEEK_API_KEY_FILE` 指向存 key 的文件；Claude 用 `ANTHROPIC_API_KEY`。仓库根目录的 `.env` 也会读。

```bash
.venv/bin/python examples/interpret.py "一个农民去侦查9点钟，同时去高地下开分基，再空投4个枪兵偷矿"
```

```bash
.venv/bin/python examples/interpret.py --run sandbox "空投金甲偷矿，顺便派个农民去对面主基地绕一圈看看"
```

```bash
docker/say.sh "一个农民去对面主基地绕一圈侦查，同时高地下开分基，再空投枪兵偷矿" --spawn MEDIVAC:1,MARINE:8
```

DeepSeek flash 一次调用约 9K 输入 token，大头是技能 schema。`--provider claude --model claude-sonnet-5-5` 切 Claude，
Claude 后端已按当前 API 写好（adaptive thinking、tool_choice auto、拒答服务端回退），但还没有 key 验证过。

## 接真游戏

技能只依赖 `WorldView` 和 `Commander` 两个协议，换游戏就是换适配器。

### 星际 2 适配器

[ai_rts/adapters/sc2.py](ai_rts/adapters/sc2.py) 用 `burnysc2` 实现了 WorldView 和 Commander，
[examples/sc2_run.py](examples/sc2_run.py) 在真游戏里下指令。依赖装在 `.venv`：

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

地图放 `maps/`（已 gitignore），来源是 Blizzard 官方 Melee 地图包。

```bash
.venv/bin/python examples/sc2_run.py --map maps/Simple64.SC2Map 'scout={"target":"enemy_main","pattern":"circle"}' expand --spawn MEDIVAC:1,MARINE:4
```

### Mac 客户端的现状（2026-10-14）

Mac 版 5.0.17（Base98310）的 API 模式目前跑不起来，不是我们代码的问题：

- 任何非 Blizzard 签名的启动方式（直接执行、`open`、launchd、复刻 Switcher 的 NSWorkspace 调用）都在启动完成瞬间 `_exit(1)`。只有 Blizzard 的 SC2Switcher 或调试器能让它活下来，是启动器身份检查。
- 经 SC2Switcher 启动时 `-listen` 被解析但被忽略，走正常登录界面，不开 API 端口。
- 在 lldb 下进入 API 模式的黑屏等待状态，但 150 秒内也不开端口。

所以真游戏测试走 Linux 无头客户端，同一份适配器不用改，只是执行环境换到容器里。

### Docker 无头客户端（Apple Silicon 上已跑通）

镜像基于 `burnysc2/python-sc2-docker`（SC2 4.10 Linux 版，amd64，自带地图），在 colima 里用 Rosetta 跑。
SC2 4.10 在 Rosetta 下加载地图时会 SIGBUS，原因是它有些 mutex 只按 2 字节对齐，Rosetta 翻成 ARM 原子指令跨 16 字节边界就炸。
[docker/alignmutex.c](docker/alignmutex.c) 是 aiarena 社区的修法（来自
[aiarena/local-play-bootstrap#22](https://github.com/aiarena/local-play-bootstrap/pull/22)），镜像里编译好，
只给 SC2 进程 `LD_PRELOAD`。x86 宿主机把 `SC2_LD_PRELOAD` 设成空就不加载。

```bash
colima start --vz-rosetta
```

```bash
docker build --platform linux/amd64 -t ai-rts-sc2 -f docker/Dockerfile .
```

```bash
docker/run.sh 'scout={"target":"enemy_main","pattern":"circle"}' expand 'drop_harass={"cargo":{"specs":[{"type":"MARINE","count":4}]}}' --spawn MEDIVAC:1,MARINE:4 --save-replay replays/test.SC2Replay
```

没有画面，但 replay 存在 `replays/`，拖进 Mac 客户端就能看。三条指令并发，一局 113 游戏秒在 12 秒真实时间里跑完：

```
[  772   34.5s] drop_harass#3  INFO     phase     到达目标，卸货
[  836   37.3s] drop_harass#3  NOTICE   phase     部队落地，开始骚扰
[  864   38.6s] drop_harass#3  INFO     kill      击杀 PROBE
[  876   39.1s] drop_harass#3  NOTICE   phase     触发撤退条件，接走部队
[ 1204   53.8s] drop_harass#3  NOTICE   done      空投 enemy_mineral_line：击杀 1，损失 0，部队已撤回
[ 1508   67.3s] scout#1  NOTICE   done      侦查 enemy_main：看到 建筑 GATEWAY、NEXUS、PYLON，单位 PROBE
[ 2544  113.6s] expand#2  NOTICE   done      my_natural 分基建成
```

### 星际 1

BWAPI 只有 Windows。Mac 上可行的是 OpenBW（引擎重实现，原生编译）加 1.16.1 的 MPQ 文件，或者 Windows 机器跑 BWAPI 再用网络桥接。放在星际 2 验证之后。
