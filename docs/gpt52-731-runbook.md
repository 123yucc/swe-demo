# GPT-5.2 SWE-bench Pro 081-731

## 启动或续跑

在服务器 `/home/user/demo` 执行：

```bash
bash scripts/start_gpt52_731.sh
```

重复执行同一命令不会启动重复 supervisor。上一次已经退出时，它会重新扫描
`outputs_gpt-5.2`，跳过已有完整 phase3 产物的 case，只准备和运行未完成项。

## 实时监控

```bash
watch -n 10 python3 scripts/gpt52_731_status.py
```

退出 `watch` 使用 `Ctrl-C`，不会停止后台任务。原始 supervisor 日志位于：

```text
runtime/gpt52-731/supervisor.log
```

## 资源策略

- 每批最多 40 个 case，完成后删除该批 repo，metadata 和所有模型/eval 产物保留。
- analysis 使用 8 workers，不创建 Docker 容器；真实 agent 请求在 24 路时触发 pending-request 429，不能按最小探测并发设置。
- 普通 case 的 stage2/phase3 使用 3 workers，每容器 6 GiB。
- 大 repo 和已知大 case 使用 2 workers，每容器 8 GiB。
- Docker 同时设置相等的 `--memory` 和 `--memory-swap`。
- 普通组完成 phase3 和镜像清理后才启动大 case 组，避免镜像同时堆积。
- 准备 repo 前要求至少 80 GiB 可用，准备后要求至少 60 GiB；不满足时该批失败并保留状态。在明确保留全部 repo 且已核验容量时，可用 `GPT52_MIN_FREE_BEFORE_GIB` / `GPT52_MIN_FREE_AFTER_GIB` 为单次 supervisor 启动覆盖门槛；默认值不变。

`resolved=false` 是正式 pass@1 结果，视为已完成，不会自动重跑。只有缺失 analysis
handoff、有效 patch/compile 或 phase3 eval 的 case 才会在下一次启动时续跑。
