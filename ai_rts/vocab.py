"""游戏词表：玩家黑话 → 内部名。

理解层把它渲染进系统提示，LLM 据此做 grounding。技能参数用的是内部单位名和区域名，
玩家说的是"枪兵""高地下""偷矿"，中间这层翻译就靠这张表。
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Vocab:
    game: str
    race: str
    units: dict[str, str]       # 黑话 → 内部单位名
    regions: dict[str, str]     # 黑话 → NamedRegion 的值
    tactics: dict[str, str]     # 黑话 → 对应技能和常用参数
    notes: tuple[str, ...] = ()

    def render(self) -> str:
        lines = [f"游戏：{self.game}，玩家种族：{self.race}", "", "单位黑话 → 内部名："]
        lines += [f"  {k} → {v}" for k, v in self.units.items()]
        lines += ["", "位置黑话 → 区域名："]
        lines += [f"  {k} → {v}" for k, v in self.regions.items()]
        lines += ["", "战术说法 → 技能："]
        lines += [f"  {k} → {v}" for k, v in self.tactics.items()]
        if self.notes:
            lines += ["", "注意："] + [f"  - {n}" for n in self.notes]
        return "\n".join(lines)


_REGIONS_COMMON = {
    "主基地 / 主矿 / 家里": "my_main",
    "二矿 / 分基 / 分矿 / 高地下 / 自然扩张": "my_natural",
    "三矿": "my_third",
    "坡口 / 高地口 / 家门口": "my_ramp",
    "我方矿区 / 我家矿线": "my_mineral_line",
    "对方主基地 / 对面家里 / 对方主矿": "enemy_main",
    "对面二矿 / 对方分基": "enemy_natural",
    "对面三矿": "enemy_third",
    "对方坡口": "enemy_ramp",
    "对方矿区 / 对面矿线 / 对方农民那": "enemy_mineral_line",
    "对方主力 / 敌军大部队": "enemy_army",
    "地图中间 / 中路": "map_center",
    "N 点钟方向": '{"hour": N}，比如 9 点钟就是 {"hour": 9}',
}

_TACTICS_COMMON = {
    "侦查 / 侦察 / 探路 / 去看一眼 / 看看对面": "scout；绕一圈看清楚用 pattern=circle，看一眼就回用 peek，盯住不走用 stay",
    "开矿 / 开分基 / 开二矿 / 扩张": "expand；没说位置就是 my_natural，开三矿是 my_third",
    "空投 / 偷矿 / 骚扰矿区 / 空降 / 打农民": "drop_harass；偷矿就是 target=enemy_mineral_line，focus=workers",
}

SC2_TERRAN = Vocab(
    game="星际争霸 2",
    race="人族 Terran",
    units={
        "农民 / SCV": "SCV",
        "枪兵 / 机枪兵": "MARINE",
        "掠夺者 / 火车": "MARAUDER",
        "死神 / 收割者": "REAPER",
        "恶火 / 火车头": "HELLION",
        "坦克": "SIEGETANK",
        "雷神": "THOR",
        "医疗艇 / 医疗运输机 / 运输机 / 飞机": "MEDIVAC",
        "维京": "VIKINGFIGHTER",
        "女妖": "BANSHEE",
        "解放者": "LIBERATOR",
        "大和": "BATTLECRUISER",
        "基地 / 指挥中心": "COMMANDCENTER",
        "兵营": "BARRACKS",
        "工厂": "FACTORY",
        "星港 / 机场": "STARPORT",
        "对方探机 / 对面农民（神族）": "PROBE",
        "狂热者 / 叉子 / XX": "ZEALOT",
        "追猎 / 追猎者": "STALKER",
    },
    regions=_REGIONS_COMMON,
    tactics=_TACTICS_COMMON,
    notes=(
        "运输单位是 MEDIVAC，技能里用角色名 transport 即可，不用写死。",
        "星际 1 的说法照样理解：金甲 = 星际 2 没有 Goliath，用枪兵或掠夺者替代并在回复里说一句。",
        "装货用 cargo.specs，例如 4 个枪兵：{\"specs\": [{\"type\": \"MARINE\", \"count\": 4}]}。",
        "空投没说数量就装满：MEDIVAC 能装 8 个枪兵。",
        "回复里一定要复述你理解成了什么，替换过单位要说明。",
    ),
)

SANDBOX_TERRAN = Vocab(
    game="沙盒（星际 1 风格的简化模拟器）",
    race="人族",
    units={
        "农民 / SCV": "SCV",
        "枪兵 / 机枪兵": "MARINE",
        "金甲 / 歌利亚": "GOLIATH",
        "运输机 / 空投机": "DROPSHIP",
        "基地 / 指挥中心": "COMMAND_CENTER",
        "对方探机 / 对面农民": "PROBE",
        "狂热者 / 叉子": "ZEALOT",
    },
    regions=_REGIONS_COMMON,
    tactics=_TACTICS_COMMON,
    notes=("运输单位是 DROPSHIP，技能里用角色名 transport 即可。",
           "空投没说数量就装满：DROPSHIP 能装 4 个单位。",
           "回复里一定要复述你理解成了什么，替换过单位要说明。"),
)


def for_game(game: str) -> Vocab:
    return {"sc2": SC2_TERRAN, "sandbox": SANDBOX_TERRAN}[game]
