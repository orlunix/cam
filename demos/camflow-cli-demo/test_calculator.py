"""Tests for calculator — these expose the bugs."""
import pytest
from calculator import add, subtract, multiply, divide, average, factorial, power


def test_add():
    assert add(2, 3) == 5
    assert add(-1, 1) == 0


def test_subtract():
    assert subtract(5, 3) == 2


def test_multiply():
    assert multiply(3, 4) == 12
    assert multiply(0, 5) == 0


def test_divide():
    assert divide(10, 2) == 5.0
    assert divide(7, 2) == 3.5


def test_divide_by_zero():
    with pytest.raises(ValueError, match="Cannot divide by zero"):
        divide(1, 0)


def test_average():
    assert average([1, 2, 3]) == 2.0
    assert average([10, 20]) == 15.0


def test_average_empty():
    with pytest.raises(ValueError, match="Cannot average empty list"):
        average([])


def test_factorial():
    assert factorial(0) == 1
    assert factorial(1) == 1
    assert factorial(5) == 120


def test_factorial_negative():
    with pytest.raises(ValueError, match="negative"):
        factorial(-1)


def test_power():
    assert power(2, 3) == 8
    assert power(5, 0) == 1


def test_power_negative_exp():
    assert power(2, -1) == 0.5
    assert power(4, -2) == 0.0625
