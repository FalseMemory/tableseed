"""split 拆分 —— 把父表的一个值分摊到 N 个子行，且 **Σ子 = 父** 严格守恒。

典型场景：一笔 1000 元交易拆成 3 笔明细，合计必须还是 1000；
银行对账场景里这个守恒是硬要求，差一分钱就是对不上的账。

为什么用「分单位整数 + 割点法」
--------------------------------
浮点直接切（``total / 3`` 再凑）会产生 333.33 + 333.33 + 333.34 这类
"看运气"的结果，份多或数值大时累计误差足以破坏守恒。

做法：先把金额换成**最小单位整数**（元 → 分，scale=2 即 ×100），
在 ``[1, total)`` 里取 ``parts - 1`` 个不重复割点，相邻割点的差就是每份 ——
每份必然为正、加总必然精确等于总额，零浮点误差。

``ratio`` 模式：按占比切前 N-1 份，最后一份兜差 —— 占比声明得再怪，守恒不破。
"""

from __future__ import annotations

from ..errors import GenerateError
from ..rng import SeededRandom

__all__ = ["split_value"]


def split_value(
    total: float | int,
    parts: int,
    rng: SeededRandom,
    scale: int = 2,
    ratio: list[float] | None = None,
    path: str = "",
) -> list[float]:
    """把 ``total`` 拆成 ``parts`` 份，返回各份（Σ = total，误差为 0）。

    - ``ratio`` 给定 → 按占比切，最后一份兜差
    - ``ratio`` 缺省 → 割点法随机均分
    """
    if parts < 1:
        raise GenerateError(f"split 的 parts 必须 >= 1，收到 {parts}", path)

    unit = 10**scale
    total_int = round(total * unit)
    if total_int < parts:
        raise GenerateError(
            f"split 拆分失败: 总额 {total} 拆 {parts} 份, 每份不足最小单位",
            path,
        )

    if parts == 1:
        return [round(total_int / unit, scale)]

    pieces: list[int]
    if ratio:
        if len(ratio) != parts:
            raise GenerateError(
                f"split 的 ratio 有 {len(ratio)} 项, 与 parts={parts} 不一致",
                path,
            )
        # 前N-1份按占比取整，最后一份兜差 —— 守恒由兜差保证
        pieces = [round(total_int * r) for r in ratio[:-1]]
        pieces.append(total_int - sum(pieces))
        if any(p <= 0 for p in pieces):
            raise GenerateError(
                f"split 的 ratio 使某份 <= 0: {ratio}（总额 {total}）",
                path,
            )
    else:
        # 割点法：parts-1 个不重复割点，相邻差即每份
        cuts = sorted(rng.rand_sample(range(1, total_int), parts - 1))
        bounds = [0, *cuts, total_int]
        pieces = [bounds[k + 1] - bounds[k] for k in range(parts)]

    return [round(p / unit, scale) for p in pieces]
