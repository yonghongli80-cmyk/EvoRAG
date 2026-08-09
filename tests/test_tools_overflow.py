"""Tests for calculate overflow / DoS protection (Task 1.5).

The ast.Pow evaluation previously had no exponent or digit-size limit, so
expressions like ``9**9**9`` could hang the worker / exhaust memory. These
tests assert such expressions are rejected quickly with an error string.
"""
import time

import pytest

from rag.tools import calculate

# 错误返回串 "计算被拒绝：指数或结果超出安全范围" 的关键词
OVERFLOW_MARKERS = ("拒绝", "超出", "过大", "范围")


def _quick_calculate(expr, timeout=2.0):
    """Run calculate(expr), asserting it returns within ``timeout`` seconds."""
    start = time.monotonic()
    result = calculate(expr)
    elapsed = time.monotonic() - start
    assert elapsed < timeout, f"calculate({expr!r}) 耗时 {elapsed:.2f}s，疑似挂死"
    return result


class TestCalculateOverflow:
    def test_nested_pow_rejected(self):
        # 9**(9**9) 原本会算出 3.69 亿位的结果，必须被快速拒绝
        result = _quick_calculate("9**9**9")
        assert isinstance(result, str)
        assert any(m in result for m in OVERFLOW_MARKERS)

    def test_right_assoc_pow_rejected(self):
        # 2**2**2**2**2 = 2**65536，指数超限，快速返回错误
        result = _quick_calculate("2**2**2**2**2")
        assert any(m in result for m in OVERFLOW_MARKERS)

    def test_huge_exponent_rejected(self):
        result = _quick_calculate("10**1000000")
        assert any(m in result for m in OVERFLOW_MARKERS)

    def test_large_result_rejected(self):
        # 2**1000 指数未超限，但结果位数超限（>10**6），同样被拒
        result = _quick_calculate("2**1000")
        assert any(m in result for m in OVERFLOW_MARKERS)

    def test_normal_computation_still_works(self):
        result = calculate("(1200-1068)/1068*100")
        assert "12.36" in result
        assert calculate("2**10") == "1024"

    def test_division_by_zero_still_handled(self):
        assert calculate("1/0") == "错误：除数不能为零"

    def test_dangerous_expression_still_rejected(self):
        # 不允许的表达式（函数调用/属性访问）仍应抛 ValueError，不回归
        with pytest.raises(ValueError):
            calculate("__import__('os').system('rm -rf /')")
