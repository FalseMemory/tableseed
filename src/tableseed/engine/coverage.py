"""覆盖策略：决定有限取值组的组合如何被展开成骨架行。

三种策略（``limits.strategy``）：

=========  ==================================================================
full       完整笛卡尔积。行数 = 组合数，组合覆盖 100%。默认。
sample     随机抽 ``sample_size`` 行。行数可控，但组合覆盖不保证。
pairwise   贪心配对覆盖。行数 ≈ O(因子数 × 最大取值数)，**两两组合覆盖 100%**。
=========  ==================================================================

pairwise 的定位（tech-design 5.5）：组合数爆炸时的实用妥协 ——
全组合覆盖不可承受时，保证**任意两个有限组的取值配对**都至少出现一次。
银行场景里「状态 × 渠道」「币种 × 渠道」两两组合有测试价值，
四五个组的全乘积往往没有。

实现采用**随机贪心**而非完整 IPOG：
每轮采样一批候选行，选覆盖未覆盖配对最多的一行加入结果，直到配对全覆盖。
贪心只依赖「配对集合」的大小（O(因子² × 取值²)），**不需要枚举全组合** ——
组合数百万级也能跑。
"""

from __future__ import annotations

from itertools import combinations
from typing import Any, Iterator

from ..errors import ConfigError
from ..models import GroupSpec
from ..rng import SeededRandom
from .group_expander import expand_skeleton

__all__ = ["expand_by_strategy", "pairwise_pairs_total", "pairwise_skeletons", "sample_skeletons"]

#: 候选行采样数 —— 每轮随机生成多少个候选行供贪心挑选
_CANDIDATES_PER_ROUND = 80


def expand_by_strategy(
    groups: list[GroupSpec],
    strategy: str,
    sample_size: int | None,
    rng: SeededRandom,
    max_rows: int,
) -> Iterator[dict[str, Any]]:
    """按策略展开骨架行。``groups`` 为空时交给调用方处理（yield 空行）。"""
    if not groups:
        yield {}
        return

    if strategy == "full":
        yield from expand_skeleton(groups)
    elif strategy == "sample":
        yield from sample_skeletons(groups, sample_size or 10, rng)
    elif strategy == "pairwise":
        yield from pairwise_skeletons(groups, rng, max_rows)
    else:
        raise ConfigError(f"不支持的覆盖策略: {strategy}")


def sample_skeletons(
    groups: list[GroupSpec], size: int, rng: SeededRandom
) -> Iterator[dict[str, Any]]:
    """随机抽 ``size`` 个组合（有放回，重复概率极低；可复现）。"""
    choices = [[tuple(t) for t in (g.values or [])] for g in groups]
    for _ in range(size):
        row: dict[str, Any] = {}
        for group, options in zip(groups, choices):
            for field, value in zip(group.fields, rng.rand_choice(options)):
                row[field] = value
        yield row


def pairwise_pairs_total(groups: list[GroupSpec]) -> int:
    """需要覆盖的取值配对总数：C(因子数, 2) 对组 × 各取值数的乘积。"""
    counts = [len(g.values or []) for g in groups]
    total = 0
    for i, j in combinations(range(len(groups)), 2):
        total += counts[i] * counts[j]
    return total


def pairwise_skeletons(
    groups: list[GroupSpec], rng: SeededRandom, max_rows: int
) -> Iterator[dict[str, Any]]:
    """贪心生成覆盖全部两两取值配对的行集合。

    返回的行数通常远小于全组合数；``max_rows`` 同时是安全阀。
    """
    options = [[tuple(t) for t in (g.values or [])] for g in groups]

    # 空组合守卫（与 group_expander 行为一致）
    if any(not v for v in options):
        return

    # 单因子没有「配对」可言 —— 每个取值一行即为最优
    k = len(groups)
    if k <= 1:
        for value in options[0] if options else []:
            row: dict[str, Any] = {}
            for field, v in zip(groups[0].fields, value):
                row[field] = v
            yield row
        return

    # 未覆盖的配对集合，键为 (组i, 取值a, 组j, 取值b)，i < j
    uncovered: set[tuple[int, int, int, int]] = set()
    for i, j in combinations(range(k), 2):
        for a in range(len(options[i])):
            for b in range(len(options[j])):
                uncovered.add((i, a, j, b))

    produced: list[tuple[int, ...]] = []

    # 首行直接取每个因子第 0 个取值，确定性强
    current = tuple(0 for _ in range(k))
    while uncovered:
        uncovered -= _pairs_covered(current, uncovered)
        produced.append(current)

        if not uncovered or len(produced) >= max_rows:
            break

        # 定向锚定：从未覆盖配对中挑一个，构造「以它为锚」的候选行 ——
        # 每轮至少消化一个配对，收敛有保证；其余因子随机、采样择优。
        i, a, j, b = rng.rand_choice(sorted(uncovered))
        best: tuple[int, ...] | None = None
        best_hits = -1
        for _ in range(_CANDIDATES_PER_ROUND):
            candidate = [rng.rand_int(0, len(v) - 1) for v in options]
            candidate[i], candidate[j] = a, b
            candidate = tuple(candidate)
            if candidate in produced:
                continue  # 锚配对未覆盖 ⇒ 锚行必然未产生过；仅防理论边角
            hits = len(_pairs_covered(candidate, uncovered))
            if hits > best_hits:
                best, best_hits = candidate, hits

        current = best if best is not None else tuple(
            a if idx == i else b if idx == j else rng.rand_int(0, len(v) - 1)
            for idx, v in enumerate(options)
        )

    for combo in produced:
        row: dict[str, Any] = {}
        for group_index, value_index in enumerate(combo):
            for field, value in zip(groups[group_index].fields, options[group_index][value_index]):
                row[field] = value
        yield row


def _pairs_covered(row: tuple[int, ...], uncovered: set) -> set:
    """一行组合能覆盖掉的未覆盖配对。"""
    k = len(row)
    hits: set = set()
    for i, j in combinations(range(k), 2):
        key = (i, row[i], j, row[j])
        if key in uncovered:
            hits.add(key)
    return hits
