# SWE-bench Pro 四阶段运行手册

本文是本项目批量评测的主操作手册。新 session 应先阅读本文，不要直接使用
旧的 `--phase all` 本地全流程。

## 总体架构

| 阶段 | 工作内容 | Docker | 推荐并发 | 主要产物 |
| --- | --- | --- | ---: | --- |
| 1. analysis | parser、deep search、静态/AST grounding、closure | 不访问 Docker daemon | GPT 8，Claude 16 | `checkpoint.json`、`analysis_stage.json` |
| 2. patch-static | patch、静态 patch closure、focused compile | 服务器 case 容器 | 普通 3，大 case 2 | `patch.diff`、`compile_check.json` |
| 3. dynamic-eval | requirement 动态 closure、官方本地测试 | 同一个受限 case 容器 | 普通 3，大 case 2 | `dynamic_closure.json`、`eval_result/eval_summary.json` |
| 4. eval-modal（暂缓） | 配置 Modal 后对同一 patch 做云端复核 | Modal 云端镜像 | Modal workers 20 起步 | `workdir/modal_eval/.../results` |

保留 `--phase all` 仅为向后兼容。新实验应使用四阶段模式，避免在耗时最长的
deep search 期间一直持有多个 6 GiB case 容器。

## 基本原则

- 正式指标是 pass@1：每个 case 只生成一次提交 patch。阶段 3 对冻结 patch 做动态
  closure 和官方本地评测；配置 Modal 后可在阶段 4 对同一 patch 做云端复核。
- 本地或 Modal eval 输出只用于统计和事后诊断，不反馈给同一次提交继续修 patch。
- API key 只通过服务器 `.env` 或进程环境变量注入，不写进 manifest、日志或文档。
- analysis、stage2、phase3、eval-modal 必须使用相同 model、backend、API surface 和
  `output_subdir`。
- 每批 manifest 只列入本批 case；不要让两个 runner 同时处理同一 case。
- 阶段 2 无论成功或失败都必须在 `finally` 清理 generation/evaluation 容器和任务镜像。
- Docker 操作统一在 `user@172.28.8.77` 的服务器执行；本机只负责 SSH、代理隧道和
  轻量代码测试，不在本机拉取或运行 case 镜像。

## 32 GiB 服务器并发配置

### 阶段 1：GPT 推荐 8 路，Claude 暂用 16 路

阶段 1 不拉镜像、不创建 generation/eval 容器、不运行编译器和官方测试，因此
内存占用比旧的本地全流程大幅下降。Qwen experience server 是宿主机全局单例，
4 路检索实测峰值约 2.54 GiB，不会随 worker 数复制为多份。2026-07-16 的 GPT
实测中，48 个并发 `src.main` 的 agent/runner RSS 约 8.9 GiB，宿主仍有约 21 GiB
available，swap 增量约 34 MiB。64 个 GPT `src.main` 也已实际同时存活，宿主仍有
约 20 GiB available。每增加一个 analysis worker，主要增加一组宿主 Python、
SDK/CLI 子进程和源码读取缓存。64 路探测只验证 GPT/Responses 链路；Claude 每
worker 还会启动 SDK/CLI 子进程，在完成独立容量实验前不能直接套用 GPT 上限。

在当前 31 GiB 可用物理内存的服务器上采用以下档位：

| 档位 | `--max-workers` | 用途 |
| --- | ---: | --- |
| 首次观察 | 24 | 新模型、新 API endpoint 或刚更新代码时先跑 10 至 20 分钟 |
| GPT 长期默认 | 8 | 24 路真实 agent 请求触发 pending-request 429；最小 ping 探测不代表完整 agent 负载 |
| 短时探测 | 48 | 已通过内存测试；是否长期使用取决于 API 错误率和吞吐增益 |
| GPT 内存上限探测 | 64 | 已通过内存测试，不作为吞吐默认 |
| Claude 默认/故障降级 | 16 | Claude 尚未完成同规格容量实验；也作为 GPT 故障降级值 |

从 40 升到 48 或维持 40 需要同时满足：

- `free -h` 的 `available` 持续不低于 12 GiB；
- swap 使用量不持续增长；
- SSH 交互正常；
- 模型 API 没有持续 rate limit/timeout；
- experience server 没有积压大量长时间 `/search`。

若 `available` 降到 8 至 12 GiB，保持当前并发，不再升档；若低于 8 GiB、swap
连续增长或 SSH 明显卡顿，熔断当前批次并降到 16。阶段 1 通常先受 API 限流和 Claude
子进程数影响，而不是受 Qwen 内存影响。

### 阶段 2：普通 3 路，大 case 2 路

普通 case 每 worker 限制 6 GiB，3 路容器上限合计 18 GiB；大 case 每 worker
限制 8 GiB，2 路容器上限合计 16 GiB。两档都给宿主 Python、共享 Qwen、Docker
daemon 和文件缓存保留约 10 GiB 以上余量。不要在这台服务器上把 4 路作为长期默认。

### 阶段 3：dynamic closure + eval，普通 3 路，大 case 2 路

阶段 3 每个 worker 只持有一个设置了相等 memory/memory-swap 的 case 容器。容器内
依次运行 base 动态测试、patched 动态测试和官方 evaluator，不允许同时创建第二个
同 case 容器。

正式 matrix 把这组值写在 manifest 的 `defaults.capacity`。环境变量
`ANALYSIS_WORKERS`、`STAGE2_WORKERS`、`PHASE3_WORKERS`、`HEAVY_WORKERS`、
`NORMAL_PER_TASK_GB`、`HEAVY_PER_TASK_GB` 可作单批覆盖。大 case 列表通过
`HEAVY_ISSUES` 注入；未知 case 先按普通档运行，出现 OOM/exit 137 后加入列表重跑。

### 阶段 4：Modal 20 路起步（账号配置后）

Modal worker 不消耗本机 Docker 内存。本地只保留提交进程和结果下载，可从
`--num-workers 20` 起步，再根据 Modal quota、费用和 registry rate limit 调整。

## 前置条件

1. 每个 case 已有 `workdir/swe_issue_NNN/artifacts/instance_metadata.json`。
2. 第一阶段还需要 `workdir/swe_issue_NNN/repo`。已有 case 无需拉镜像；新 case
   若没有 repo，仍需先 clone 对应 base commit 或拉一次镜像抽取 `/app`。
3. 三个阶段必须使用同一个 manifest 和相同模型配置。checkpoint 会校验 backend、
   model 和 API surface，配置不一致时拒绝恢复。
4. 服务器 `.env` 已配置模型 API；Qwen experience server 数据位于
   `workdir/long_term_memory`。
5. 第四阶段执行前安装并登录 Modal：

```bash
python3 -m pip install modal
modal setup
```

若通过环境变量提供凭据，必须确保变量已导出给 Python 子进程：

```bash
test -n "$MODAL_TOKEN_ID" && test -n "$MODAL_TOKEN_SECRET"
export MODAL_TOKEN_ID MODAL_TOKEN_SECRET
```

阶段 4 wrapper 会在提交前检查凭据，并在结束后要求每个 case 都有真实
`_output.json`。因此 Modal 认证或基础设施异常不会再被上游 evaluator 的 exit 0
误记为普通 unresolved。

服务器基础检查：

```bash
free -h
df -h . /var/lib/docker
docker info
docker ps
docker images
```

服务器已有完整 LTM 缓存时，建议显式禁止 HuggingFace 联网探测，避免离线服务器
启动时卡住：

```bash
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1
```

### 代理与 Docker pull

阶段 1 不需要 Docker pull。阶段 2 的 Docker daemon 若配置为使用
`127.0.0.1:7897`，必须从本机保持 OpenSSH 反向隧道：

```bash
ssh -N -T -R 7897:127.0.0.1:7897 user@172.28.8.77
```

OpenSSH `-R` 比 Paramiko 转发更适合大镜像下载。出现
`proxyconnect tcp: dial tcp 127.0.0.1:7897: connect: connection refused` 时检查：

```bash
ss -ltnp | grep 7897
docker pull hello-world
```

`docker pull hello-world` 是阶段 2 的强制代理预检。它失败时先恢复隧道，不要启动
会拉取数 GiB case 镜像的 generate runner。

### Case 准备

每个 case 至少应包含：

```text
workdir/swe_issue_NNN/
  artifacts/instance_metadata.json
  repo/.git
```

服务器不能稳定访问 HuggingFace 时，可从已有 metadata 拉镜像并抽取 `/app`：

```bash
python3 scripts/fetch_issues_docker.py \
  --from-existing-metadata \
  --start-label <START_LABEL> \
  --count <COUNT>
```

脚本会尝试 `jefzda/sweap-images` 和 `123yucc/sweap-images`，抽取 repo 后应删除
准备阶段镜像。批量检查：

```bash
for n in $(seq <START> <END>); do
  i=$(printf '%03d' "$n")
  test -f workdir/swe_issue_${i}/artifacts/instance_metadata.json || echo "missing metadata $i"
  test -d workdir/swe_issue_${i}/repo/.git || echo "missing repo $i"
done
```

### Manifest 约束

- 不要把 API key 写进 manifest。
- `issues` 只包含计划运行的 case，便于断点续跑和审计。
- `output_subdir` 固定使用模型相关目录，例如 `outputs_claude-sonnet-4.5`。
- 当前 OpenAI-compatible gateway 不支持 `previous_response_id`，会返回
  `previous_response_not_found`；GPT-5.2 manifest 必须使用 stateless replay。
  Agents SDK 仍可使用，只需设置 `OPENAI_AGENT_LOOP=agents_sdk`。
- `remote_run_staged_batch.sh` 优先读取单模型 `capacity`，再读取
  `defaults.capacity`，最后使用 GPT analysis=8、Claude analysis=16 的 backend
  默认值。直接调用 runner 且不传 `--max-workers` 时仍使用保守的 analysis=8、
  generate=2；旧 `--phase all` 继续采用 manifest 的 `max_workers`。

## 阶段 1：高并发 analysis

示例 manifest：`eval/local_manifest.claude-sonnet-4.5.021-080.json`。首次针对旧输出
重跑时使用 `--force-restart`；正常断点续跑不要加它。

```bash
cd /home/user/demo
bash scripts/remote_start_runner.sh \
  eval/local_manifest.claude-sonnet-4.5.021-080.json \
  claude45-analysis-021-080 \
  --phase analysis \
  --max-workers 16
```

查询状态和停止：

```bash
bash scripts/remote_runner_status.sh claude45-analysis-021-080
bash scripts/remote_stop_runner.sh claude45-analysis-021-080
```

运行日志统一位于：

```text
logs/runs/claude45-analysis-021-080/
  runner.log
  runner.pid
  runner.status
  runner.state.json
```

每个成功 case 必须同时满足：

- `checkpoint.json` 的 `pipeline_state` 为 `Closed`。
- `budget_counters.dynamic_grounding_done` 为 `false`。
- `analysis_stage.json` 为 `analysis_complete` 且
  `dynamic_grounding_deferred=true`。

此阶段不会创建容器、拉镜像、生成 patch 或执行编译。阶段耗时和 token 指标写入
`run_metrics.analysis.json`，第二阶段不会覆盖它。

analysis 若 closure 未通过必须以失败结束，且不得留下 `patch.diff`、
`prediction.json` 或其他可被阶段 2/3 误认成有效 patch 的产物。只有上述
`Closed` 且 deferred-grounding 的 checkpoint 才能进入阶段 2。

## 阶段 2：低并发 patch-static

阶段 2 强制要求阶段 1 的有效 checkpoint。不要对 generate 使用
`--force-restart`；如需完全重做，先用 `--phase analysis --force-restart` 重建
checkpoint。

```bash
cd /home/user/demo
bash scripts/remote_start_runner.sh \
  eval/local_manifest.claude-sonnet-4.5.021-080.json \
  claude45-stage2-021-080 \
  --phase stage2 \
  --max-workers 2 \
  --per-task-gb 6 \
  --no-prune
```

`stage2` 只完成 patch、静态 patch closure 和 focused compile，不运行动态测试或
官方 evaluator。旧 dynamic grounding 已从正式状态机删除，Closed checkpoint 直接
进入 patch planning。`--no-prune` 只保留 task image 供紧接着的 phase3 复用；容器仍
在 finally 中清理。phase3 完成后默认统一删除镜像。

恢复 checkpoint 后，状态机会先回到 `EvidenceRefining`，在 case 镜像中补跑
dynamic grounding，再运行一次 closure。之后才进入 patch planner、patch generator
和 build verify/repatch。成功后删除 checkpoint，保留：

```text
workdir/swe_issue_NNN/outputs_<model>/
  patch.diff
  prediction.json
  patch_outcome.json
  run_metrics.analysis.json
  run_metrics.json
  logs/generate.log
```

generate 中断后直接重跑同一 generate 命令。首次从 analysis 的 `Closed` checkpoint
接棒时才补跑 deferred dynamic grounding；一旦 checkpoint 记录
`dynamic_grounding_done=true`，后续恢复应从当前 generation 状态继续，不能再次传入
或触发 `--resume-deferred-dynamic-grounding`。

容器内存限制必须同时约束 memory 与 memory-swap。以 4 GiB smoke 为例，运行中核验：

```bash
docker inspect -f \
  'Memory={{.HostConfig.Memory}} MemorySwap={{.HostConfig.MemorySwap}} OOMKilled={{.State.OOMKilled}} Status={{.State.Status}}' \
  <generation-container>
```

应看到 `Memory=4294967296`、`MemorySwap=4294967296` 和 `OOMKilled=false`。正式批次可按
case 使用 `--per-task-gb 4` 至 `6`，但不得省略限制；先从 4 GiB、单 worker smoke 开始。

`run_metrics.json` 是 generate 阶段指标；总成本分析时应同时读取 analysis 和
generate 两个 metrics 文件。

## 阶段 3：dynamic closure + 官方本地 eval

阶段 3 只读取阶段 2 冻结的 patch，不允许修改或重新生成：

```bash
bash scripts/remote_start_runner.sh \
  eval/local_manifest.claude-sonnet-4.5.021-080.json \
  claude45-phase3-021-080 \
  --phase phase3 \
  --max-workers 2 \
  --per-task-gb 6
```

`phase3` 按 parent contract 批量生成测试，每个 RequirementItem 独立判定。测试生成器
只能读取 base tree、requirement/source span 和 evidence，不能读取 patch、gold patch、
`test_patch` 或官方 oracle。同一冻结测试先在 base tree、再在 patched tree 执行；无论
动态 closure 结果如何，都继续官方 eval，且不反馈修 patch。

阶段 3 产物：

```text
dynamic_closure.json
logs/dynamic_closure.log
eval_result/eval_summary.json
logs/eval.log
```

阶段 2 generation 容器用于：

- dynamic grounding 的语言原生 runner；
- patch 编辑后的 syntax validation；
- build verify 和失败后的 repatch 反馈。

## 阶段 4：Modal 云端复核（暂缓）

在注册 Modal 账号并配置 token 前保留本节代码，不执行本阶段。配置完成后仍使用
阶段 2 生成的同一份 patch，不允许根据本地评测结果修改后再提交。

先只生成输入并检查命令，不提交 Modal：

```bash
cd /home/user/demo
python3 eval/modal_swebench_runner.py \
  --output-subdir outputs_claude-sonnet-4.5 \
  --all \
  --num-workers 20 \
  --prepare-only
```

确认 `patches.json` 数量和目标 case 正确后，去掉 `--prepare-only`：

```bash
python3 eval/modal_swebench_runner.py \
  --output-subdir outputs_claude-sonnet-4.5 \
  --all \
  --num-workers 20
```

只评测指定 case：

```bash
python3 eval/modal_swebench_runner.py \
  --output-subdir outputs_claude-sonnet-4.5 \
  --issues swe_issue_021 swe_issue_022 swe_issue_027 \
  --num-workers 10
```

默认目录：

```text
workdir/modal_eval/outputs_claude-sonnet-4.5/
  inputs/patches.json
  inputs/samples.jsonl
  results/
```

该入口调用 `eval/SWE-bench_Pro-os/swe_bench_pro_eval.py`，且不会传
`--use_local_docker`，所以走官方 Modal 路线。输入收集器只接受真实生成的
`patch.diff`；缺 patch 的 case 会跳过，绝不会回退到 metadata 中的 gold patch。

## 断点与重跑

- analysis 中断：原命令重跑，不加 `--force-restart`，从每个 case 最近 checkpoint
  继续。
- analysis 想完全重做：使用 `--phase analysis --force-restart`。
- generate 中断：原 generate 命令重跑；有 analysis checkpoint 的 case 会恢复，
  已有有效 `patch.diff` 的 case 会跳过。
- stage2 中断：原 stage2 命令重跑；有 generation checkpoint 时继续生成，已有 patch
  的 case 跳过。
- generate checkpoint 被破坏：先重跑对应 case 的 analysis，再运行 generate。
- phase3 中断：重跑同一 `--phase phase3` 命令；冻结 patch 不变。
- Modal 中断：重复第四阶段命令；默认复用已有输出，需要强制重评时加 `--redo`。
- 不要在 analysis 和 generate 两个 runner 中同时调度同一 case，它们共享 repo 工作树。

## 开跑前检查

```bash
free -h
docker ps
df -h / /var/lib/docker
python3 eval/local_swebench_runner.py --help
python3 eval/modal_swebench_runner.py --help
```

阶段 1 期间 `docker ps` 应为空。阶段 2 从 2 workers 开始，用以下命令监控：

```bash
watch -n 1 'free -h; docker stats --no-stream'
```

阶段 1 不需要 `docker stats`，重点观察宿主进程、available memory、swap 和 API：

```bash
watch -n 2 'free -h; uptime; pgrep -af "src.main|claude|experience_server" | wc -l'
tail -f logs/runs/<analysis-run-name>/runner.log
```

## 故障判断与降级

- 出现 `exit 137`、OOM、Docker daemon 无响应：停止新增阶段 2 任务，下次降到
  1 至 2 路。
- SSH 开始卡顿或 `banner exchange` 超时：优先按宿主机内存压力处理，不要先假定
  是网络故障。
- `available` 长期过低且 swap 持续增长：降低并发；不要只看 `free` 列，因为
  Linux 文件缓存可回收。
- Docker pull 报代理拒绝：先修复 7897 隧道，不要反复重启 runner。
- 单个 case 的 build/test 特别重：为这些 case 建独立小 manifest，以 1 路运行。
- analysis 只出现 API rate limit 而内存充足：降低到 6 或 4，增加并发不会提高吞吐。

## 结果收集与结束审计

生成阶段指标可继续使用：

```bash
python3 scripts/collect_metrics.py \
  --output-subdir outputs_claude-sonnet-4.5 \
  --issues <ISSUE_LIST> \
  --output-dir workdir/metrics_claude-sonnet-4.5
```

统计口径：阶段 3 官方本地 evaluator 的 `resolved/pass@1` 是当前正式结果；Modal
以后只复核同一 patch。`patch_outcome`、compile outcome 和 dynamic closure 只作
过程诊断，不能代替 pass@1。总成本应合并 analysis、patch 和 dynamic closure 指标。

每批结束检查：

```bash
docker ps -a
docker images
df -h . /var/lib/docker
find workdir/modal_eval -path '*/results/*' -type f | wc -l
```

至少核验以下产物：analysis checkpoint 数、有效 `patch.diff` 数、提交 Modal 的
patch 数、Modal 返回结果数、resolved 数和 unresolved case 列表。Docker 命令日志
必须对 `OPENAI_API_KEY`、`ANTHROPIC_API_KEY` 等 secret 脱敏。

维护代码时运行项目测试必须限制到顶层 `tests/`，否则 pytest 会递归收集
`workdir/swe_issue_*/repo` 内各上游项目自己的测试：

```bash
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python3 -m pytest -q tests
```
