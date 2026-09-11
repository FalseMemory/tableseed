"""种子化随机源。

**必须**使用独立 Random 实例而非模块级 random 函数 —— 否则其他代码随手
调用一次 ``random.random()`` 就会污染序列，破坏可复现性（PRD NFR-2）。
"""

from __future__ import annotations

import random
import uuid as _uuid
import zlib

_HASH_BITS = 0xFFFFFFFF


def stable_hash(key: str, seed: int = 0) -> int:
    """跨进程稳定的字符串散列。

    **绝不能**用内置 ``hash()`` —— 它对 str/bytes 受 PYTHONHASHSEED 随机化
    影响，同一 key 在不同进程会得到不同值，直接破坏「同 seed 结果可复现」
    （PRD NFR-2）。这里改用 zlib.crc32，任何进程、任何 Python 版本都一致。
    """
    return zlib.crc32(f"{seed}:{key}".encode("utf-8")) & _HASH_BITS


class SeededRandom:
    """可复现的随机源。同一 seed 永远产出同一序列。"""

    def __init__(self, seed: int) -> None:
        self.seed = seed
        self._rng = random.Random(seed)

    def rand_int(self, low: int, high: int) -> int:
        return self._rng.randint(int(low), int(high))

    def rand_decimal(self, low: float, high: float, scale: int = 2) -> float:
        value = self._rng.uniform(float(low), float(high))
        return round(value, int(scale))

    def rand_choice(self, candidates: list, weights: list[float] | None = None):
        if not candidates:
            raise ValueError("rand_choice 的候选集不能为空")
        if weights:
            return self._rng.choices(candidates, weights=weights, k=1)[0]
        return self._rng.choice(candidates)

    def rand_sample(self, population, k: int) -> list:
        """无放回抽样 k 个（用于 split 的割点法）。"""
        return self._rng.sample(list(population), k)

    def rand_uuid(self) -> str:
        """确定性 UUID —— 由种子派生，保证可复现。"""
        return str(_uuid.UUID(int=self._rng.getrandbits(128), version=4))

    def fork(self, key: str) -> "SeededRandom":
        """按 key 派生子随机源，使不同表的随机序列互不干扰。

        使用 :func:`stable_hash` 而非内置 ``hash``，保证跨进程可复现。
        """
        return SeededRandom(stable_hash(key, self.seed))
