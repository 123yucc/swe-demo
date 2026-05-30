# 拉取新 SWE-bench Pro Case 指导书

## 概述

本项目的 case 存放在 `workdir/swe_issue_NNN/`，每个目录对应 SWE-bench Pro 数据集
（`ScaleAI/SWE-bench_Pro`，split=`test`）的一个 instance。

**编号规则：** `swe_issue_NNN` 对应数据集 index `NNN - 1`。

| swe_issue 编号 | dataset index | 说明 |
|---|---|---|
| swe_issue_001 | 0 | 第 1 个 case |
| swe_issue_015 | 14 | 当前最新 |
| swe_issue_016 | 15 | 下一批起点 |

## 目录结构

每个 case 的标准结构：

```
workdir/swe_issue_NNN/
├── artifacts/
│   └── instance_metadata.json   # 完整 instance 元数据（从 HuggingFace 下载）
└── repo/                        # 代码仓库，已 reset 到 base_commit
    └── .git/
```

`instance_metadata.json` 关键字段：

| 字段 | 说明 |
|---|---|
| `instance_id` | 唯一标识符 |
| `repo` | GitHub 仓库路径，如 `ansible/ansible` |
| `base_commit` | 需要 reset 到的 commit hash |
| `patch` | gold patch（正确答案） |
| `problem_statement` | 问题描述 |
| `dockerhub_tag` | Docker 镜像 tag（不含用户名前缀） |
| `before_repo_set_cmd` | 评测前需要执行的 git 命令 |
| `fail_to_pass` | 需要从失败变通过的测试 |
| `pass_to_pass` | 需要保持通过的测试 |

## 拉取脚本

**主脚本：** `scripts/fetch_issues_docker.py`

> `scripts/fetch_issues.py` 是早期版本，依赖直连 GitHub（国内网络不可用），不要使用。

### 前置条件

1. Docker Desktop 已启动（任务栏有 Docker 图标）
2. 已安装 `datasets` 库：`pip install datasets`
3. D 盘有足够空间（每个 case 的镜像约 1.5–8 GB，提取后 repo 约 50–600 MB）

### 基本用法

```bash
# 拉取下一批 5 个 case（接着 015 往后，即 dataset index 15-19）
python scripts/fetch_issues_docker.py --start 15 --count 5 --start-label 16

# 只处理单个 case（调试或补拉某一个）
python scripts/fetch_issues_docker.py --start 15 --count 5 --start-label 16 --only 17
```

### 参数说明

| 参数 | 含义 | 示例 |
|---|---|---|
| `--start` | dataset 起始 index（0-based） | `15` |
| `--count` | 拉取数量 | `5` |
| `--start-label` | 第一个 case 的编号 | `16` |
| `--only` | 只处理指定编号（跳过其他） | `17` |
| `--workdir` | 输出目录，默认 `workdir` | `workdir` |

**计算公式：** `--start` = `--start-label` - 1

### 执行流程

脚本对每个 case 依次执行：

1. **下载 metadata** — 从 HuggingFace 加载 dataset，保存到 `artifacts/instance_metadata.json`
2. **拉取 Docker 镜像** — 从 `jefzda/sweap-images:<dockerhub_tag>` 拉取
3. **提取 repo** — 容器内 `cp -rL` 解引用 symlink，再 `docker cp` 到宿主机
4. **reset 到 base_commit** — 本地 git `reset --hard` + `clean -fd`

### 磁盘管理

镜像较大（1.5–8 GB），提取完 repo 后建议立即删除：

```bash
# 查看本地镜像
"C:/Program Files/Docker/Docker/resources/bin/docker.exe" images

# 删除已提取完的镜像
"C:/Program Files/Docker/Docker/resources/bin/docker.exe" rmi jefzda/sweap-images:<tag>

# 清理悬空资源
"C:/Program Files/Docker/Docker/resources/bin/docker.exe" system prune -f
```

## 验证

拉取完成后验证结构完整性：

```python
python -c "
import json
from pathlib import Path

for i in range(16, 21):  # 调整范围
    meta = Path(f'workdir/swe_issue_{i:03d}/artifacts/instance_metadata.json')
    repo = Path(f'workdir/swe_issue_{i:03d}/repo')
    has_meta = meta.exists()
    has_git = (repo / '.git').exists()
    status = 'OK' if has_meta and has_git else 'MISSING'
    if has_meta:
        d = json.load(open(meta, encoding='utf-8'))
        print(f'[{i:03d}] {status}  {d[\"repo\"]}  base={d[\"base_commit\"][:8]}')
    else:
        print(f'[{i:03d}] {status}')
"
```

## 运行 harness

```bash
python -m src.main \
    --instance-json workdir/swe_issue_016/artifacts/instance_metadata.json \
    --repo-dir workdir/swe_issue_016/repo
```

## 常见问题

**`docker cp` 报 symlink 权限错误**
脚本已通过 `cp -rL` 规避。如仍失败，检查 Docker Desktop 是否以管理员权限运行。

**镜像拉取失败（not found）**
镜像存放在 `jefzda/sweap-images`，tag 取自 `instance_metadata.json` 的 `dockerhub_tag` 字段。
确认 Docker Desktop 网络正常，以及 tag 拼写正确。

**D 盘空间不足**
提取完 repo 后立即删除镜像（见"磁盘管理"）。

**`swe_extract_tmp` 容器冲突**
脚本会自动清理，也可手动执行：
```bash
"C:/Program Files/Docker/Docker/resources/bin/docker.exe" rm -f swe_extract_tmp
```

**base_commit reset 失败**
手动执行：
```bash
git -C workdir/swe_issue_NNN/repo reset --hard <base_commit>
git -C workdir/swe_issue_NNN/repo clean -fd
```
