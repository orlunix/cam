import sys


def calc(a, op, b):
    if op == '+':
        return a + b
    elif op == '-':
        return a - b
    elif op == '*':
        return a * b
    elif op == '/':
        if b == 0:
            print("错误: 除数不能为零")
            sys.exit(1)
        return a / b
    else:
        print(f"错误: 不支持的运算符 '{op}'")
        sys.exit(1)


def main():
    if len(sys.argv) != 4:
        print("用法: python calc.py <数字> <运算符> <数字>")
        print("示例: python calc.py 3 + 5")
        sys.exit(1)

    try:
        a = float(sys.argv[1])
        b = float(sys.argv[3])
    except ValueError:
        print("错误: 请输入有效的数字")
        sys.exit(1)

    op = sys.argv[2]
    result = calc(a, op, b)
    print(f"{a} {op} {b} = {result}")


if __name__ == "__main__":
    main()

