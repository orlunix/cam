"""Simple calculator with bugs for the workflow demo to fix."""


def add(a, b):
    return a + b


def subtract(a, b):
    return a - b


def multiply(a, b):
    return a * b


def divide(a, b):
    if b == 0:
        raise ValueError("Cannot divide by zero")
    return a / b


def average(numbers):
    if not numbers:
        raise ValueError("Cannot average empty list")
    return sum(numbers) / len(numbers)


def factorial(n):
    if n < 0:
        raise ValueError("Cannot compute factorial of negative number")
    result = 1
    for i in range(1, n + 1):
        result *= i
    return result


def power(base, exp):
    return base ** exp
