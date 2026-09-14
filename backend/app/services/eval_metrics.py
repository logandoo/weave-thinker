# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""评测指标纯函数（E2/CAST 借鉴，2026-09-14）。

- pass_hat_k：τ-bench（arXiv 2406.12045）的可靠性估计
  P(random size-k subset of n trials all-pass) = C(c, k) / C(n, k)；
  与 pass@k（至少一次成功）相反，pass^k 随 k 下降，度量"每次都行"。
"""
from math import comb

# E2（2026-09-14）：失败 taxonomy（死磕 judge + 审计判词共用；CAST 推理侧 4 条）
TAXONOMY_VALUES = ("hallucination", "domain", "wrong-tool", "other")


def pass_hat_k(rewards: list, k: int) -> float:
    """单任务 n 次试验的 pass^k 无偏估计；k>n 时返回 0.0（τ-bench 约定）。"""
    n = len(rewards or [])
    if k <= 0 or k > n:
        return 0.0
    c = sum(1 for r in rewards if r)
    return comb(c, k) / comb(n, k)


def pass_hat_k_multi(task_rewards: list[list], k: int) -> float:
    """多任务宏平均（每任务 trials 列表 → 各任务 pass^k → 均值）。"""
    vals = [pass_hat_k(r, k) for r in (task_rewards or []) if len(r or []) >= k]
    if not vals:
        return 0.0
    return round(sum(vals) / len(vals), 4)


def pass_hat_k_curve(task_rewards: list[list], ks: list[int] | None = None) -> dict:
    """报告曲线：k → 宏平均 pass^k；k 超过最小任务试验数则跳过。"""
    ks = ks or [1, 2, 4, 8]
    max_n = min((len(r) for r in task_rewards if r), default=0)
    return {k: pass_hat_k_multi(task_rewards, k) for k in ks if k <= max_n}
