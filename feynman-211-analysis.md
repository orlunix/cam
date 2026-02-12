# 📊 Feynman-211 Project Analysis Summary

**Project Location**: `/home/scratch.hren_gpu/test/fd/feynman-211_peregrine_add_memory_ecc`  
**Remote Host**: pdx-container-xterm-110.prd.it.nvidia.com  
**Analysis Date**: 2026-02-11 10:27 PST  
**Session**: analyze-1770834407

---

## 🎯 Project Overview

### Purpose
升级 NVIDIA GPU 中 **Peregrine RISC-V 核心**的内存保护机制：
- **从**: Parity（仅检测错误）
- **到**: SEC-DED ECC（单错纠正-双错检测）

### Business Impact
- **问题**: 数据中心大规模部署 GPU，单比特错误导致 GPU 重启，任务中断
- **FIT Rate**: 每个 GPU ~800 FIT，Peregrine 贡献 ~10 FIT
- **解决方案**: ECC 可自动纠正单比特错误，无需重启
- **收益**: 零停机时间，集群利用率提升，任务迁移开销降低

---

## 📂 Project Structure

```
feynman-211_peregrine_add_memory_ecc/
├── README.md                          # 项目概述（5.4KB）
├── CLAUDE.md                          # Claude AI 指导文档（3.3KB）
├── Feynman-211_..._TP.xlsm           # 测试计划（Excel，89KB）
├── Feynman-211_..._TP.adoc           # 测试计划（AsciiDoc）
├── Feynman-211_..._TP_Requirements.adoc  # 需求工作表
├── Feynman-211_..._TP_Functional.adoc    # 功能测试工作表
├── tools/
│   └── gen_tp.py                      # TP 生成脚本（298行，13KB）
└── reference/
    ├── Arch_Process_TP.md             # TP 流程指南
    ├── TP_Template.xlsm               # 官方模板
    ├── Blackwell-1174_..._peregrine.xlsm  # Blackwell 参考 TP
    ├── ecc_template.yml               # ECC 模板配置
    ├── feynman-211-fd/                # 功能设计文档
    │   ├── index.adoc
    │   ├── 00_preamble.adoc
    │   ├── 00_glossary_and_acronyms.adoc
    │   ├── 01_overview.adoc
    │   └── 02_functional_description.adoc
    └── feynman-211-plus/              # 额外技术内容
```

---

## 🔧 Technical Details

### Affected Components

#### Memory Types (4 categories)
1. **ICache, DCache**: 32-bit + 7-bit ECC
2. **L1TCM (IMEM/DMEM)**: 32-bit + 7-bit ECC
3. **L2TCM (UTCM)**: 64-bit + 8-bit ECC
4. **MPU**: 64-bit + 8-bit ECC

#### Affected Engines (9 units)
| Engine | Change | RAMs | MPU Count | Interrupt Path |
|--------|--------|------|-----------|----------------|
| **MSE** | Parity → ECC | ICache, DCache, IMEM, DMEM, UTCM, MPU | 4 | Legacy GIN |
| **GSP** | Parity → ECC | ICache, DCache, IMEM, DMEM, UTCM, MPU, KMEM | 4 | GIN safety |
| **PMU** | Parity → ECC | ICache, DCache, IMEM, DMEM, UTCM, MPU | 2 | GIN safety |
| **SEC** | Parity → ECC | IMEM, DMEM, MPU, KMEM | 1 | GIN safety |
| **FSP** | Parity → ECC | IMEM, DMEM, MPU | 1 | GIN safety |
| **PXUC** | Parity → ECC | IMEM, DMEM, UTCM, MPU | 1 | Legacy GIN |
| **FECS** | Parity → ECC | IMEM, DMEM | 0 | Legacy GIN |
| **GPCCS** | Parity → ECC | IMEM, DMEM | 0 | Legacy GIN |
| **OOB** | **None → ECC** | IMEM, DMEM | 0 | GIN safety (new) |

### Interrupt Architecture (2 distinct paths)
1. **GIN Safety Path**: GSP, SEC, PMU, FSP, OOB
   - error_collator → GIN_plugin_in_peregrine → GIN
2. **Legacy GIN Path**: MSE, PXUC, FECS, GPCCS
   - error_collator → GIN_plugin_in_engine → GIN

---

## 📋 Test Plan Structure

### Requirements Coverage
- **Total Requirements**: 45 (9 engines × 5 requirements each)
- **ID Convention**: `Feynman-211:REQ:1` to `REQ:45`

#### Per-Engine Requirements (5 each)
| Req Type | ID | Description |
|----------|-----|-------------|
| **DREQ_48** | REQ:N | 中断延迟 <10μs（从单元到软件）|
| **DREQ_49** | REQ:N+1 | 报告延迟与工作负载无关 |
| **VREQ_9** | REQ:N+2 | 假注入到达芯片边界（所有 RAM）|
| **VREQ_11** | REQ:N+3 | 所有 RAM 实例覆盖（不仅实例 0）|
| **VREQ_14** | REQ:N+4 | 正常流量无误报 ECC 错误 |

### Functional Tests
- **Total Tests**: 18 (9 engines × 2 tests each)
- **ID Convention**: `Feynman-211:1` to `Feynman-211:18`
- **Pattern**: 每个引擎 2 个测试

### Scope Division
| Owner | Scope |
|-------|-------|
| **This TP** | 单元级集成需求（中断路径、延迟、实例覆盖）|
| **::psw plugin** | ECC 核心功能（编码/解码、计数器、地址、初始化、fuse）|

---

## 🛠️ Development Workflow

### Primary Command
```bash
python3 tools/gen_tp.py
```

### Pipeline Architecture
```
gen_tp.py (数据源头)
    ↓
Blackwell 参考 xlsm (base template)
    ↓
生成 Feynman-211 xlsm (保留 VBA 宏)
    ↓
手动维护 adoc 文档（并行）
```

### Document Formats
1. **xlsm**: Excel 可执行工作簿，带 VBA 宏
   - Per-engine 行格式（45 行需求）
   - 官方审查格式
2. **adoc**: AsciiDoc 文档
   - 合并行格式（5 行需求，每行列出所有单元）
   - 易于版本控制和审查

### Key Scripts
- **gen_tp.py** (298 lines):
  - 复制 Blackwell xlsm 作为 base
  - 清除 Blackwell 数据
  - 填充 Feynman-211 需求（45 行）
  - 填充功能测试（18 个测试）
  - 保留所有格式、VBA、sheet 结构

---

## 📈 Project Status

### Documentation Status
| Document | Status | Sign-off Stage |
|----------|--------|----------------|
| **FD** (Functional Description) | Draft 0.1 | In progress → 0.7 → 1.0 |
| **TP** (Test Plan) | Draft | Active development |

### Git History (10 most recent commits)
```
fced8ea  Add KMEM back for GSP and SEC
d313b58  Remove KMEM from GSP and SEC RAM lists per updated FD
30cee83  Fix Change History: replace Blackwell history with Feynman-211
45f416f  Drop plugin-owned reqs, keep 45 per-engine only; update FD docs
da0651d  Update README to reflect new directory structure
7310145  Reorganize: move FD/plus to reference/, gen_tp.py to tools/
fa9cbe2  Add gen_tp.py script for reproducible xlsm generation
3d9aa5e  Reorganize TP: split adoc by worksheet, add Functional tests
8d88631  Rebuild xlsm from Blackwell base, fix MS_DEF tabs only
1f91dd1  Regenerate xlsm from official TP_Template, add reference docs
```

### Recent Changes Pattern
- 项目结构重组（FD/tools 目录）
- gen_tp.py 脚本开发（可重现生成）
- Blackwell 参考清理
- KMEM 配置调整（GSP/SEC）
- 文档格式规范化

---

## 👥 Team & Ownership

| Role | Owner | Contact |
|------|-------|---------|
| **FD Author** | Jason Xiong | jasonx@nvidia.com |
| **TP Author** | Huailu Ren (hren) | hren@nvidia.com |
| **SysArch** | Yanxiang Huang, Philip Shirvani | - |
| **Peregrine DV** | Liqi Zhao, Iry Feng | ifeng@nvidia.com |

---

## 🔍 Key Insights

### Project Maturity
- **Well-structured**: 清晰的文档层次和工具链
- **Automated**: gen_tp.py 实现可重现构建
- **Reference-driven**: 基于 Blackwell 成熟模板
- **Version-controlled**: Git 历史清晰，commit 规范

### Technical Sophistication
- **Multi-engine coverage**: 9 个不同的 GPU 引擎
- **Dual interrupt paths**: 安全路径 vs 遗留路径
- **Comprehensive RAM types**: 4 类内存，不同 ECC 配置
- **Separation of concerns**: TP 关注集成，plugin 处理核心功能

### Development Approach
- **Template-based**: 复用 Blackwell 经验
- **Tooling-first**: 脚本化生成，避免手动错误
- **Documentation-parallel**: xlsm (官方) + adoc (协作)
- **Incremental refinement**: 多次重构优化结构

### Claude AI Integration
- **CLAUDE.md** 提供 AI 辅助指导
- 明确工作流和命令
- 架构和模式说明
- 为 AI 协作优化的项目

---

## 💡 Recommendations

### Short-term (Ready)
1. ✅ 项目结构完整，可以开始详细测试用例编写
2. ✅ gen_tp.py 工具成熟，可用于批量更新
3. ✅ 文档框架完备，FD 0.1 可推进到 0.7

### Medium-term (Next steps)
1. 完成 FD 0.7 sign-off（初始设计）
2. TP 功能测试细化（18 个测试的详细步骤）
3. 与 ::psw plugin 团队同步 ecc_template.yml

### Long-term (Planning)
1. FD 1.0 final sign-off
2. DV 环境验证
3. 硅后测试准备

---

## 📊 Statistics

### Code & Documentation
- **Python**: 298 lines (gen_tp.py)
- **AsciiDoc**: ~15 files (FD + TP)
- **Excel**: 89KB (TP xlsm)
- **Total Project Size**: ~140KB (without .git)

### Coverage
- **Engines**: 9
- **Requirements**: 45
- **Tests**: 18
- **RAM Types**: 4 categories, 20+ instances
- **Interrupt Paths**: 2

---

## 🎯 Summary

**Feynman-211** 是一个**成熟的、工具化的、文档驱动**的 GPU 可靠性提升项目。

### Strengths ✅
- 📚 **Documentation Excellence**: 完整的 FD/TP/README/CLAUDE 文档
- 🔧 **Automation**: gen_tp.py 实现可重现构建
- 🏗️ **Architecture**: 清晰的模块化和责任分离
- 📦 **References**: 基于成熟的 Blackwell 模板
- 🤖 **AI-Ready**: CLAUDE.md 优化 AI 协作

### Current State 📍
- FD: Draft 0.1 (进行中)
- TP: Draft (活跃开发)
- 工具链: 完整可用
- 团队: 明确分工

### Impact 🚀
- **Technical**: 从 Parity 升级到 SEC-DED ECC
- **Business**: 减少 GPU 重启，提升数据中心可靠性
- **Scale**: 9 个引擎，45 个需求，18 个测试

**项目已准备好进入下一个 milestone（FD 0.7）！** ✨

---

**Analysis completed**: 2026-02-11 10:28 PST  
**Remote Session**: analyze-1770834407 @ pdx-container-xterm-110  
**Status**: ✅ Active and healthy
