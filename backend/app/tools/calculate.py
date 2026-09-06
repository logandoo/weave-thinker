# Copyright (c) 2026 Weave Thinker Contributors
# SPDX-License-Identifier: Apache-2.0

"""calculate tool — deterministic math expression evaluation (NPG P1/D).

conv 83d97ede (2026-09-01): the writer model computed a frustum volume by
pure mental arithmetic (≈40ml vs true ≈186.5ml, 4.66× off) because the
execute_code path is heavyweight (LLM codegen + sandbox roundtrip), so the
model skipped it for "simple" math. calculate gives arithmetic a
millisecond-scale deterministic outlet: the model writes the EXPRESSION
(expression-driven by construction — the input numbers must appear in it,
so `print(40000)`-style hardcoding cannot launder a hallucinated value
through it), and the runtime computes.

Security: Python ast.parse + strict node whitelist (arithmetic operators,
numeric constants, whitelisted math functions/constants only) — no
attribute access, no strings, no subscripts, no names outside the
whitelist, no eval. Injection-proof by construction. stdlib-only (A6:
no new dependency; symbolic equation solving via sympy deferred to P2).
"""
import ast
import json
import logging
import math
from typing import Any, Dict

from app.tools.registry import registry

logger = logging.getLogger(__name__)


class _CalcError(Exception):
    pass


_ALLOWED_FUNCS = {
    "sqrt": math.sqrt, "sin": math.sin, "cos": math.cos, "tan": math.tan,
    "asin": math.asin, "acos": math.acos, "atan": math.atan, "atan2": math.atan2,
    "log": math.log, "log2": math.log2, "log10": math.log10, "exp": math.exp,
    "fabs": math.fabs, "floor": math.floor, "ceil": math.ceil, "round": round,
    "degrees": math.degrees, "radians": math.radians, "hypot": math.hypot,
    "factorial": math.factorial, "gcd": math.gcd,
}
_ALLOWED_CONSTS = {"pi": math.pi, "e": math.e, "tau": math.tau}

_SUSPICIOUS_TOKENS = ("__", "import", "eval", "exec", "lambda", "getattr",
                      "setattr", "compile", "open", "system", "subprocess")


def _eval_node(node: ast.AST):
    """Ints stay ints (exact big-int arithmetic; factorial/gcd/round usable —
    A4.9 I4); floats stay floats; division promotes to float."""
    if isinstance(node, ast.Expression):
        return _eval_node(node.body)
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or not isinstance(node.value, (int, float)):
            raise _CalcError("仅允许数字常量")
        return node.value
    if isinstance(node, ast.BinOp):
        left = _eval_node(node.left)
        right = _eval_node(node.right)
        try:
            if isinstance(node.op, ast.Add):
                return left + right
            if isinstance(node.op, ast.Sub):
                return left - right
            if isinstance(node.op, ast.Mult):
                return left * right
            if isinstance(node.op, ast.Div):
                return left / right
            if isinstance(node.op, ast.FloorDiv):
                return left // right
            if isinstance(node.op, ast.Mod):
                return left % right
            if isinstance(node.op, ast.Pow):
                if abs(right) > 1000 or abs(left) > 1e100:
                    raise _CalcError("幂运算规模超限")
                return left ** right
        except OverflowError as exc:
            raise _CalcError(f"数值溢出: {exc}")
        except ZeroDivisionError:
            raise _CalcError("除以零")
        raise _CalcError(f"不支持的运算符: {type(node.op).__name__}")
    if isinstance(node, ast.UnaryOp):
        operand = _eval_node(node.operand)
        if isinstance(node.op, ast.UAdd):
            return +operand
        if isinstance(node.op, ast.USub):
            return -operand
        raise _CalcError(f"不支持的一元运算符: {type(node.op).__name__}")
    if isinstance(node, ast.Name):
        if node.id in _ALLOWED_CONSTS:
            return _ALLOWED_CONSTS[node.id]
        raise _CalcError(f"未知符号「{node.id}」——只允许数学常量 {sorted(_ALLOWED_CONSTS)}")
    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name) or node.func.id not in _ALLOWED_FUNCS:
            raise _CalcError("只允许白名单内的数学函数")
        if node.keywords:
            raise _CalcError("不支持关键字参数")
        args = [_eval_node(a) for a in node.args]
        if node.func.id == "factorial":
            if not args or args[0] != int(args[0]) or abs(args[0]) > 10000:
                raise _CalcError("factorial 仅接受 ≤10000 的整数")
        try:
            return float(_ALLOWED_FUNCS[node.func.id](*args))
        except (ValueError, OverflowError, TypeError) as exc:
            raise _CalcError(f"函数 {node.func.id} 参数错误: {exc}")
    raise _CalcError(f"不允许的语法节点: {type(node).__name__}")


async def calculate(args: Dict[str, Any], **kwargs) -> str:
    expression = str(args.get("expression") or "").strip()
    if not expression:
        return json.dumps(
            {"success": False, "error": "未提供 expression（数学表达式，如 3.14159*110/3*(32.5**2+32.5*12.5+12.5**2)）"},
            ensure_ascii=False,
        )
    lowered = expression.lower()
    if any(tok in lowered for tok in _SUSPICIOUS_TOKENS):
        return json.dumps(
            {"success": False, "error": "表达式包含被禁止的词法成分（仅允许纯数学表达式）"},
            ensure_ascii=False,
        )
    try:
        tree = ast.parse(expression, mode="eval")
        value = _eval_node(tree)
    except SyntaxError as exc:
        return json.dumps({"success": False, "error": f"表达式语法错误: {exc.msg}"}, ensure_ascii=False)
    except _CalcError as exc:
        return json.dumps({"success": False, "error": str(exc)}, ensure_ascii=False)
    except RecursionError:
        return json.dumps({"success": False, "error": "表达式嵌套过深"}, ensure_ascii=False)
    if value != value or value in (float("inf"), float("-inf")):
        return json.dumps({"success": False, "error": "结果非有限数"}, ensure_ascii=False)
    exact_str = str(value)
    try:
        numeric = float(value)
        if numeric != numeric or numeric in (float("inf"), float("-inf")):
            numeric = None
    except OverflowError:
        numeric = None
    display = f"{numeric:.6f}".rstrip("0").rstrip(".") if (
        numeric is not None and abs(numeric) < 1e15
    ) else exact_str
    return json.dumps(
        {
            "success": True,
            "expression": expression,
            "numeric": numeric,
            "exact": exact_str,
            "display": display,
            "note": "这是确定性计算结果。回答中引用该数值时请注明其为计算所得；"
                    "多步计算请继续用 calculate 逐步求值或改用 execute_code 打印中间量。",
        },
        ensure_ascii=False,
    )


registry.register(
    name="calculate",
    toolset="core",
    schema={
        "name": "calculate",
        "description": (
            "确定性数学计算器（毫秒级、精确、防心算错误）。任何含推导数值的计算——"
            "四则运算、百分比、单位换算、几何/财务/统计公式——一律先用本工具求值，"
            "严禁心算后直接作答。接受单个数学表达式（支持 + - * / // % **、"
            "sqrt/log/exp/sin/cos/factorial 等、常量 pi/e/tau）。"
            "表达式必须包含题目中的输入数值（如 65/25/110），不要写 print(结果字面量) 式的"
            "硬编码——写完整公式。多步计算可多次调用或改用 execute_code 打印公式与中间量。"
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "expression": {
                    "type": "string",
                    "description": "数学表达式，例如 3.141592653589793*110/3*(32.5**2+32.5*12.5+12.5**2)",
                },
            },
            "required": ["expression"],
        },
    },
    handler=calculate,
    is_async=True,
    description="Deterministic math expression evaluation (anti-mental-arithmetic)",
    emoji="",
)
