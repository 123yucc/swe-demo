根据 `eval/SWE-bench_Pro-os` 的代码库和测试评估脚本 `swe_bench_pro_eval.py`，如果想在本地环境评估运行一个实例的测试，你可以使用仓库内提供的评测脚本。

该脚本主要会拉取包含了整个运行环境与测试代码的 Docker 镜像，通过执行生成的代码 Patch 补丁（即模型预测出来的修改方案），然后运行这个实例内的 `run_script.sh` 测试文件，并比对测试结果是否通过。

要在本地运行并测试单个实例，你需要按以下步骤操作：

### 1. 准备工作
确保你的本地环境已经安装了 Docker 以及相关的 Python 依赖：
```bash
pip install docker pandas tqdm
```

### 2. 准备评测的数据文件
`swe_bench_pro_eval.py` 脚本期望你提供：
1. **一个 CSV 文件 (`--raw_sample_path`)**: 包含这个实例的元数据（如 `instance_id`, `before_repo_set_cmd`, `selected_test_files_to_run`, `base_commit` 等）。你可以从 HuggingFace 上下载数据集保存为 CSV 格式，或者只把这个单个实例保存为一条记录的 CSV。
2. **一个 JSON 文件 (`--patch_path`)**: 里面记录你要测试的代码补丁，格式如下（如果你想用你的修复方案测试）：
```json
[
  {
    "instance_id": "要测试的实例ID",
    "patch": "diff --git a/... b/... (你的 git 补丁内容)",
    "prefix": "test_run"
  }
]
```

### 3. 克隆仓库以获取测试运行脚本
在评估期间，脚本需要用到 `run_scripts` 和 `dockerfiles` 这两个目录里定义的辅助执行脚本（`run_script.sh`, `parser.py` 等）。在本仓库中它们位于 `eval/SWE-bench_Pro-os/` 下。
```bash
cd eval/SWE-bench_Pro-os
```

### 4. 运行评测命令
通过 `--use_local_docker` 标志来指示脚本在本地的 Docker 环境中运行（而不是使用默认的 Modal 云服务），并通过 `--dockerhub_username=jefzda` 指定官方的 Docker Hub 账号。

```bash
python swe_bench_pro_eval.py \
    --raw_sample_path=单个实例的数据.csv \
    --patch_path=你的补丁文件.json \
  --output_dir=workdir/<issue_name>/eval_result/ \
    --scripts_dir=run_scripts \
    --dockerhub_username=jefzda \
    --use_local_docker
```

### 执行过程发生了什么？
当你执行上面的命令时：
1. 脚本会根据 `instance_id` 在 Docker Hub 上去寻找对应的镜像（例如 `jefzda/sweap-images:some_tag`）。
2. 在本地通过 Docker 挂载一个 `/workspace`。
3. 把你的 `patch.diff` 和它内置的测试脚本 `run_script.sh` 与 `parser.py` 写入 workspace。
4. 容器启动后，自动打上你的代码补丁，然后运行测试 `bash /workspace/run_script.sh`。
5. 脚本最终会收集 `output.json`，并与 `fail_to_pass` 和 `pass_to_pass` 期望结果比对，最终在 `workdir/<issue_name>/eval_result/` 生成 `eval_results.json` 判定是否成功修复。