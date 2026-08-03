# SWE-bench Pro 评测管线

## 概述

本项目使用 `eval/SWE-bench_Pro-os/swe_bench_pro_eval.py` 对生成的 patch 进行 SWE-bench Pro 评测。
评测在 Docker 容器中执行：apply patch -> 运行测试 -> 解析结果 -> 判定 pass/fail。

评测结果统一存放在 `workdir/eval_result/` 目录下。

---

## 前置条件

1. 安装依赖：
```bash
pip install docker pandas datasets tqdm
```

2. 确保 Docker Desktop 已启动（本地评测模式）

3. DockerHub 镜像已推送（用户名为 `jefzda`）

---

## 管线一：单个 Case 评测

适用场景：对某一个 instance 生成的 patch 进行快速验证。

### 步骤

**1. 运行 harness 生成 patch：**
```bash
python -m src.main --instance-json workdir/swe_issue_011/artifacts/instance_metadata.json     --repo-dir workdir/swe_issue_011/repo
```
输出 patch 位于 `workdir/swe_issue_011/outputs_<model>/patch.diff`（例如 `outputs_gpt-5.2`）

**2. 准备评测输入文件：**

需要两个文件：
- `patches.json` — patch 内容
- `samples.jsonl` — instance 元数据

可使用以下 Python 脚本快速生成（以 swe_issue_001 为例）：

```bash
python eval/make_eval_inputs.py --issues swe_issue_001
```

这会在 `workdir/eval_result/` 下生成：
- `patches.json`
- `samples.jsonl`

**3. 运行评测：**
```bash
python eval/SWE-bench_Pro-os/swe_bench_pro_eval.py \
    --raw_sample_path workdir/eval_result/samples.jsonl \
    --patch_path workdir/eval_result/patches.json \
    --output_dir workdir/eval_result \
    --scripts_dir run_scripts \
    --dockerhub_username jefzda \
    --use_local_docker \
    --redo
```

> ⚠️ **`--scripts_dir` 路径坑（务必注意）**
> `swe_bench_pro_eval.py` 对**相对路径**的解析基准是脚本自身所在目录
> （`BASE_DIR = eval/SWE-bench_Pro-os`），不是当前工作目录。
> - ✅ 正确：`--scripts_dir run_scripts`（解析为 `eval/SWE-bench_Pro-os/run_scripts`）
> - ✅ 正确：传绝对路径，如 `--scripts_dir D:/demo/eval/SWE-bench_Pro-os/run_scripts`
> - ❌ 错误：`--scripts_dir eval/SWE-bench_Pro-os/run_scripts`
>   → 会被拼成 `eval/SWE-bench_Pro-os/eval/SWE-bench_Pro-os/run_scripts`（路径翻倍），
>   报错 `Script not found: ...`，且结果被记为 `false`（伪失败，patch 根本没被测试）。

**4. 查看结果：**

评测结果写入 `workdir/eval_result/eval_results.json`，格式：
```json
{
  "instance_id_1": true,
  "instance_id_2": false
}
```

每个 instance 的详细日志在 `workdir/eval_result/<instance_id>/workspace/` 下：
- `stdout.log` — 测试标准输出
- `stderr.log` — 测试标准错误
- `output.json` — parser 解析后的结构化结果

---

## 管线二：多个 Instance 批量评测

适用场景：一次性评测多个 case 的 patch。

### 步骤

**1. 确保各 issue 的 patch 已生成：**

每个 issue 目录下应有 `outputs_<model>/patch.diff` 或 `artifacts/instance_metadata.json` 中包含 gold patch。

**2. 准备评测输入文件：**
```bash
python eval/make_eval_inputs.py --issues swe_issue_001 swe_issue_002 swe_issue_003
```

默认读取当前 `.env` 模型对应的 `outputs_<model>` 目录；如需评测其他模型输出，显式指定：

```bash
python eval/make_eval_inputs.py --issues swe_issue_001 --output-subdir outputs_claude-sonnet-4.5
```

也可以用 `--all` 自动扫描所有有 patch 的 issue：
```bash
python eval/make_eval_inputs.py --all
```

**3. 运行评测：**
```bash
python eval/SWE-bench_Pro-os/swe_bench_pro_eval.py \
    --raw_sample_path workdir/eval_result/samples.jsonl \
    --patch_path workdir/eval_result/patches.json \
    --output_dir workdir/eval_result \
    --scripts_dir run_scripts \
    --dockerhub_username 123yucc \
    --use_local_docker \
    --num_workers 4 \
    --redo
```

**4. 查看结果：**

同管线一，结果在 `workdir/eval_result/eval_results.json`。

---

## 评测脚本参数说明

| 参数 | 说明 |
|------|------|
| `--raw_sample_path` | samples JSONL 文件路径 |
| `--patch_path` | patches JSON 文件路径 |
| `--output_dir` | 评测输出目录 |
| `--scripts_dir` | run_scripts 目录（含每个 instance 的 run_script.sh 和 parser.py） |
| `--dockerhub_username` | DockerHub 用户名 |
| `--use_local_docker` | 使用本地 Docker（不用 Modal） |
| `--docker_platform` | Docker 平台，如 `linux/amd64`（ARM Mac 需要） |
| `--num_workers` | 并行 worker 数（默认 50） |
| `--redo` | 强制重新评测（忽略已有结果） |
| `--block_network` | 容器内禁止网络访问 |

---

## 目录结构

```
workdir/
├── swe_issue_001/          # 单个 case 的工作目录
│   ├── artifacts/
│   │   └── instance_metadata.json
│   ├── repo/               # 克隆的目标仓库
│   └── outputs_<model>/
│       └── patch.diff      # harness 生成的 patch
├── eval_result/            # 统一评测结果目录
│   ├── patches.json        # 评测输入：patch 列表
│   ├── samples.jsonl       # 评测输入：instance 元数据
│   ├── eval_results.json   # 评测汇总结果
│   └── <instance_id>/      # 每个 instance 的详细结果
│       ├── _output.json
│       ├── _patch.diff
│       ├── _entryscript.sh
│       └── workspace/
│           ├── stdout.log
│           ├── stderr.log
│           └── output.json
eval/
├── eval.md                 # 本文档
├── make_eval_inputs.py     # 评测输入生成脚本
├── docker/                 # Docker 容器中运行 harness 的基础设施
│   ├── run_docker_issues.ps1   # 在 Docker 中顺序运行多个 issue
│   ├── setup_wheels.ps1        # 预下载 Linux wheels（运行一次）
│   └── wheels/                 # 预下载的 Python wheels
└── SWE-bench_Pro-os/       # 评测框架（git submodule）
    ├── swe_bench_pro_eval.py
    ├── helper_code/
    ├── run_scripts/
    └── dockerfiles/
```
