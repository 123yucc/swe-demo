需求：获取swe-bench-pro的第一个实例的issue描述文件和代码库、镜像文件，作为workdir的issue_003，描述文件存入workdir\swe_issue_001\artifacts，代码库存入workdir\swe_issue_001\repo，镜像文件放在workdir\swe_issue_001\下面即可。




根据 `scaleapi/SWE-bench_Pro-os` 的官方文档，要获取单个 SWE-bench Pro 实例的问题描述（issue description）、代码库（codebase）以及 Docker 镜像文件，可以通过使用 HuggingFace 的 `datasets` 库并访问预构建的 Docker 镜像来实现。

具体步骤如下：

### 1. 获取问题描述 (Issue Description) 和实例数据
SWE-bench Pro 的所有实例数据（包括问题描述、补丁等）都托管在 HuggingFace 上。你可以通过 Python 的 `datasets` 库进行加载：

```python
from datasets import load_dataset

# 加载测试集数据
dataset = load_dataset('ScaleAI/SWE-bench_Pro', split='test')

# 获取特定实例的详细信息，例如第一个实例
instance = dataset[0]

# 打印问题描述
print(instance['problem_statement'])

# 其他可用字段包括：instance_id, repo, base_commit 等
```

### 2. 获取代码库 (Codebase) 和 Docker 镜像文件
SWE-bench Pro 为每个实例都预构建了独立的 Docker 镜像。该镜像内**已经包含**了特定于该实例的**完整代码库**以及所需的运行环境。

要找到并拉取特定实例的 Docker 镜像，可以通过从 HuggingFace 数据集中读取该实例的 `dockerhub_tag`：

```python
from datasets import load_dataset

dataset = load_dataset('ScaleAI/SWE-bench_Pro', split='test')

# 获取特定实例的 Docker 镜像信息
for row in dataset:
    instance_id = row['instance_id']
    docker_tag = row['dockerhub_tag']
    full_image = f"jefzda/sweap-images:{docker_tag}"
    print(f"实例 {instance_id} 的 Docker 镜像为: {full_image}")
    break # 仅查看第一个实例
```

一旦你知道了特定的镜像名称（例如 `jefzda/sweap-images:some_tag`），你就可以直接在终端拉取并运行该镜像，从而在里面访问它的代码库：

```bash
# 拉取 Docker 镜像
docker pull jefzda/sweap-images:<对应的_docker_tag>

# 运行镜像并访问内部环境（即包含该实例代码库的环境）
docker run -it jefzda/sweap-images:<对应的_docker_tag> bash
```
*(注意：官方提示默认情况下容器就会运行 Bash，所以某些情况下不需要在最后手动加 `bash`。)*