"""Simple calculator with bugs for the workflow demo to fix."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b


def divide(a, b):
    # Bug: no zero-division check
    return a / b


def average(numbers):
    # Bug: no empty-list check
    return sum(numbers) / len(numbers)


def factorial(n):
    # Bug: off-by-one (range stops too early) + no negative check
    result = 1
    for i in range(1, n):
        result *= i
    return result


def power(base, exp):
    # Bug: manual loop can't handle negative exponents
    result = 1
    for _ in range(exp):
        result *= base
    return result
