# 2026-07-07 SWE-bench Pro OOM investigation

## Conclusion

The available logs do not prove that the kernel OOM killer ran. The supplied
`journalctl -b -1` lines only show `systemd-oomd` and containerd starting at
boot. They do not contain `Out of memory` or `Killed process`. Reading the
previous boot's kernel journal requires sudo on the server, so the killed
process, if any, remains unconfirmed.

Qwen3-Embedding-0.6B was not large enough to cause the 32 GiB host failure by
itself. A controlled CPU benchmark on the server measured the following for
the actual `experience_server.py` process:

| Phase | RSS | PSS |
| --- | ---: | ---: |
| Loaded and idle | 1,789,872 KiB (1.707 GiB) | 1,781,933 KiB (1.699 GiB) |
| After one representative `/search` | 2,649,688 KiB (2.527 GiB) | 2,641,707 KiB (2.519 GiB) |
| After four concurrent representative `/search` calls | 2,662,008 KiB (2.539 GiB) | 2,654,027 KiB (2.531 GiB) |
| Process high-water mark | 2,661,968 KiB (2.539 GiB) | n/a |

The model file is 1,191,586,416 bytes, uses bfloat16 weights, has 28 layers and
supports up to 32,768 positions. The 2.539 GiB result is the observed peak for
the project's short retrieval queries, including four simultaneous calls. It
is not a mathematical upper bound for arbitrary 32K-token input, which this
pipeline does not normally send.

The most likely pressure source is aggregate memory:

- Four generation containers can each consume up to 6 GiB: 24 GiB total.
- The shared experience server peaks near 2.54 GiB.
- Four host-side `src.main` processes and their Claude Agent SDK subprocesses
  are outside the Docker limits.
- Docker daemon/containerd, the OS and filesystem cache also need memory.

This leaves less than 5.5 GiB for all host-side agents and the OS before the
containers reach their limits. The runner's default `--reserve-gb 6` is not a
safe reserve for four Claude workers plus Qwen. On this 31 GiB host, use three
workers for normal runs and reduce to two for known heavy cases. Treat four as
a monitored probe, not a stable default.

## Corrected incident timeline

`experience_server.log` shows:

- 18:31:20 to 18:31:35: Qwen and ChromaDB loaded; the server became ready.
- 19:03:13 to 19:03:38: three `/search` calls performed embedding inference.
  They completed sequentially in 8.48 s, 3.50 s and 3.21 s.
- 19:04:11: three `/get_experience` requests returned stored JSON records.
  This endpoint does not call the embedding model.

Therefore the 19:04:11 requests were not a Qwen inference spike. The last
actual embedding inference had completed about 33 seconds earlier.

## How this project uses Docker

The active entry point is `eval/local_swebench_runner.py`, not the upstream
Modal evaluator:

1. The manifest expands into `(model, issue)` tasks and a host-side thread pool.
2. Each task resolves its prebuilt `jefzda/sweap-images:<tag>` image (with
   fallbacks), runs `docker image inspect`, and pulls it if absent.
3. The runner creates a long-lived generation container from that case image
   with `--memory=6g --memory-swap=6g` by default.
4. `src.main` and the model/agent SDK run on the host. Repository build, test
   and inspection commands are redirected into the case container with
   `docker exec` through `REPO_EXECUTOR_DOCKER_CONTAINER`.
5. After patch generation, the runner creates an evaluation container from the
   same image, mounts the evaluation workspace, applies the patch and runs the
   official tests.
6. In `finally`, it removes task containers and, unless `--no-prune` is used,
   removes images and prunes unused Docker resources.

The image is therefore more than an evaluator dependency. It is the project's
generation-time build/test sandbox and provides the exact repository checkout,
toolchain, native libraries and services for each case.

## Why Modal does not remove local Docker here

The upstream `eval/SWE-bench_Pro-os/swe_bench_pro_eval.py` supports two
evaluation backends: Modal by default and local Docker with
`--use_local_docker`. Modal runs only the official patch-evaluation sandbox in
the cloud.

This project deliberately replaced that path with a custom local runner. Its
agent generates the patch while continuously executing repository commands in
the case container, then evaluates locally. Calling the upstream Modal
evaluator only for the final test would move evaluation off-host, but would not
remove the generation container without an architectural change.

Avoiding local image pulls requires one of these designs:

- Run the whole custom generation harness and final evaluator inside Modal,
  including the experience-service network path and artifact persistence.
- Keep host-side generation but add a remote Docker/Modal executor abstraction
  for every repository command, then use Modal for final evaluation too.
- Stop using the case image during generation and operate on a plain checkout;
  this saves local Docker resources but makes build/test feedback less faithful
  and still needs Modal for the authoritative final result.

The first option is the cleanest cloud architecture, but it is a migration,
not a command-line switch in the current runner.

## Next-run checks

Use `--max-workers 3` initially. During a probe, record host and per-container
memory continuously:

```bash
watch -n 1 'free -h; docker stats --no-stream'
```

To confirm a future OOM victim, run the following with sudo before logs rotate:

```bash
sudo journalctl -k -b -1 --no-pager \
  | grep -i -E 'oom|out of memory|killed process|memory cgroup'
```

