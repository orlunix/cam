# cm-extract-code.sh 设计文档

## 设计理念

从日志文件中智能提取代码块，识别文件名，自动创建文件。

## 核心算法

### 状态机模型

```
┌─────────────┐
│   扫描文本   │
│  (in_code=false)│
└──────┬──────┘
       │
       │ 遇到 ```
       ▼
┌─────────────┐
│  读取代码块  │
│ (in_code=true) │
└──────┬──────┘
       │
       │ 遇到 ```
       ▼
┌─────────────┐
│  保存/输出   │
└─────────────┘
```

### 处理流程

```
输入: 日志文件
  ↓
逐行读取
  ↓
移除时间戳: [HH:MM:SS]
  ↓
┌─────────────────────┐
│ 检测代码块标记 ``` │
└─────┬───────────────┘
      │
      ├─ 开始标记 (```) → 进入代码模式
      │   ↓
      │   记录语言: python/bash/etc
      │   ↓
      ├─ 代码行 → 累积到 buffer
      │   ↓
      └─ 结束标记 (```) → 处理代码块
          ↓
          ┌─────────────┐
          │ 有文件名？  │
          └──┬────────┬─┘
             │ YES    │ NO
             ▼        ▼
        创建文件    输出到终端
```

## 关键组件

### 1. 时间戳清理
```bash
# 输入: [06:45:40] def calc(a, op, b):
# 输出: def calc(a, op, b):
line=$(echo "$line" | sed 's/^\[[0-9:]*\] //')
```

### 2. 代码块检测
```bash
if echo "$line" | grep -q '^```'; then
    # 是代码块标记
```

**匹配模式:**
- ````python` → 开始 Python 代码块
- ````bash` → 开始 Bash 代码块
- ` ``` ` → 结束代码块

### 3. 文件名提取

#### 模式匹配
```bash
# 检测提示词
if echo "$line" | grep -qE "创建.*文件|文件.*:|`.*\..*`"; then
    # 提取反引号中的文件名
    fname=$(echo "$line" | grep -oP '`\K[^`]+(?=`)' | grep '\.' | head -1)
fi
```

**示例输入 → 输出:**
```
创建 `calc.py` 文件          → calc.py
文件: `config.json`          → config.json
以下是 config_parser.py 的实现 → (不匹配，没有反引号)
```

### 4. 代码累积
```bash
while [ "$in_code" = true ]; do
    code_content+="$line"$'\n'
done
```

使用 `$'\n'` 确保换行符正确保留。

### 5. 文件创建
```bash
if [ -n "$current_file" ]; then
    echo "创建文件: $current_file"
    echo "$code_content" > "$current_file"
fi
```

## 实际例子

### 输入日志 (简化)
```
[06:45:38] 我会创建 `calc.py` 文件：
[06:45:39] 
[06:45:40] ```python
[06:45:40] def calc(a, op, b):
[06:45:40]     if op == '+':
[06:45:40]         return a + b
[06:45:41] ```
```

### 处理过程
```
1. 读取 "我会创建 `calc.py` 文件："
   → 检测到文件名模式
   → current_file = "calc.py"

2. 读取 "```python"
   → 开始代码块
   → in_code = true
   → current_lang = "python"

3. 读取代码行
   → code_content += "def calc(a, op, b):\n"
   → code_content += "    if op == '+':\n"
   → code_content += "        return a + b\n"

4. 读取 "```"
   → 结束代码块
   → in_code = false
   → 创建文件: calc.py
   → 写入 code_content
```

### 输出
```
创建文件: calc.py
代码提取完成
```

## 优势

### ✅ 智能识别
- 自动检测多种文件名格式
- 支持多个代码块
- 语言无关（Python/JS/Bash/等）

### ✅ 健壮性
- 时间戳不影响解析
- 代码中的反引号不会误识别（只在非代码区搜索）
- 空白行正确保留

### ✅ 灵活性
- 无文件名时输出到终端（供查看）
- 可以连续处理多个文件

## 已知限制

### ❌ 当前问题
1. **正则表达式错误** - `grep '\.'` 在某些情况下被解释为命令
   ```bash
   # 错误提示
   .*..*: command not found
   ```
   
2. **文件名检测不完美** - 可能漏掉某些格式
   ```
   "创建 server.py"  → 无反引号，不匹配
   ```

3. **路径问题** - 创建在当前目录，可能需要指定工作目录

## 改进方向

### 版本 2.0 计划

```bash
# 1. 修复正则表达式
fname=$(echo "$line" | grep -oP '`\K[^`]+(?=`)' | grep -E '\.[a-z]+' | head -1)

# 2. 多种文件名检测
detect_filename() {
    local line=$1
    
    # 方法1: 反引号
    fname=$(echo "$line" | grep -oP '`\K[^`]+(?=`)')
    [ -n "$fname" ] && echo "$fname" && return
    
    # 方法2: 常见模式
    fname=$(echo "$line" | grep -oP '\b\w+\.[a-z]{2,4}\b')
    [ -n "$fname" ] && echo "$fname" && return
}

# 3. 工作目录支持
if [ -n "$current_file" ]; then
    output_path="$workdir/$current_file"
    echo "创建文件: $output_path"
    echo "$code_content" > "$output_path"
fi

# 4. 统计信息
echo "提取完成: $file_count 个文件, $code_blocks 个代码块"
```

### 智能模式

```bash
# 自动检测常见项目结构
if echo "$current_file" | grep -q "/"; then
    # 包含路径，创建目录
    mkdir -p "$(dirname "$current_file")"
fi

# 根据语言推断文件名
if [ -z "$current_file" ] && [ -n "$current_lang" ]; then
    case "$current_lang" in
        python) current_file="untitled_$block_num.py" ;;
        bash)   current_file="untitled_$block_num.sh" ;;
        javascript) current_file="untitled_$block_num.js" ;;
    esac
fi
```

## 使用场景

### 场景 1: 单文件项目
```bash
cm start claude "创建计算器 calc.py" --ctx workspace
cm exec sess-XXX
cm extract sess-XXX  # → 创建 calc.py
```

### 场景 2: 多文件项目
```bash
cm start claude "创建 TODO API：server.py 和 models.py" --ctx webapp
cm exec sess-XXX
cm extract sess-XXX  # → 创建 server.py 和 models.py
```

### 场景 3: 查看代码不创建
```bash
# 如果没检测到文件名，只输出代码
cm extract sess-XXX | less
```

## 总结

**设计哲学**: 
- 简单 > 复杂
- 实用 > 完美
- 容错 > 严格

**核心价值**:
绕过权限限制，让 Claude 的输出变得真正有用！

**下一步**: 修复正则表达式错误，增强文件名检测。
