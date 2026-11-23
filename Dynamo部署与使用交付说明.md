# 两台 A100 的 Dynamo 部署与使用交付说明

本文记录 2026-11-19 在两台 A100 上完成的 Kubernetes + Dynamo **1.6.0** 部署，以及接手后的启停、推理和排查方法。

原部署验收后，模型服务按要求停止。当时保留了 Kubernetes、Dynamo Operator、CRD、镜像和本地模型。接手时可用 `bash deploy.sh status` 和 `kubectl get nodes -o wide` 核对现场状态。

## 1. 我们的部署过程

1. **把两台机器加入同一个 K8s 集群。** 两台都配置了 containerd、kubelet、NVIDIA Container Toolkit 和 NVIDIA Device Plugin。a100_1 通过 kubeadm 初始化控制平面，a100_2 加入为工作节点。Cilium 使用 VXLAN，两台都调度 GPU Worker。验收时两个节点各报告 8 张可分配 GPU。
2. **准备镜像和模型。** 节点通过 `crictl` 预拉镜像。SGLang 大镜像遇到下载中断后，从已有完整镜像的节点传 tar，并用 `ctr -n k8s.io images import` 导入。两台各自保留一份 `/data0/models/qwen3-0.6B`，检查了配置、tokenizer 和 safetensors 文件。
3. **安装 Dynamo 平台。** 在 a100_1 用 Helm 安装 1.6.0 的 Dynamo Platform，等待 Operator Ready 和 DGD/DGDR CRD Established。平台采用 Kubernetes 服务发现。[Helm 配置](platform-values.yaml)
4. **提交 DGD 并启动服务。** 先做 Kubernetes 服务端 dry run，再提交 DGD 和 NodePort Service。Pod 只读挂载本地模型。16 个 SGLang Worker 每卡一个，Prefill 和 Decode 在同一个 Worker 上执行。一个 Frontend 为两台机器统一路由。[DGD 和 Service 清单](qwen3-0.6b-16gpu.yaml)
5. **验证并停止。** 确认 16 个 Worker 都被发现，两台机器各有 8 个 Worker。两个 NodePort 地址均可访问，普通推理和流式推理通过。32 个并发测试请求全部成功，日志显示每个 Worker 各处理了 2 个。验收后停止模型服务，16 张 GPU 的显存均已释放。

从新机器复现时，按裸机准备、建集群、准备镜像与模型、启动及验收的顺序执行。[可迁移说明](可迁移说明.md)

### 部署配置

| 项目 | 配置 |
|---|---|
| 服务器 / K8s 节点名 | a100_1 / `a100-1`；a100_2 / `a100-2` |
| 内网 IP | `10.1.22.129`；`10.1.22.130` |
| GPU | 每台 8 张 A100-SXM4-80GB，共 16 张 |
| Kubernetes / containerd | `v1.35.10` / `2.2.1` |
| Dynamo Platform、Frontend、SGLang Runtime | 统一 `1.6.0` |
| 推理方式 | P/D 共置，16 个 Worker，每个 Worker 1 张 GPU，TP=1 |
| Frontend | 1 个 Pod，统一为两台机器的 Worker 路由请求 |
| 路由 | KV 感知路由；SGLang 发布 KV 事件 |
| 模型 | `Qwen/Qwen3-0.6B` |
| 宿主机模型目录 | 两台均为 `/data0/models/qwen3-0.6B`，各自独立存储 |
| 容器内模型目录 | `/models/qwen3-0.6B`，Frontend 和 Worker 均只读挂载 |
| API | NodePort `30080`；服务名 `qwen3-api` |
| 服务发现 / 请求 / 事件通信 | Kubernetes / TCP / ZMQ |

K8s 负责调度 Pod 和分配 GPU；Frontend 中的 Dynamo Router 负责选择推理 Worker；Worker 中的 SGLang 执行模型计算。两台机器部署独立的单卡副本，单个模型副本不做跨节点 TP。

此配置固定 Worker 数量，Planner/Profiler、DGDR 自动选型、P/D 分离、HiCache、Ingress/TLS 均未启用。DGD/DGDR CRD 已安装，后续可以另行配置这些功能。

### 文件与前置条件

从新裸机开始部署，请先阅读 [可迁移说明.md](可迁移说明.md)。脚本、配置和清单都在仓库根目录，复制到服务器时放在同一个目录。

| 文件 | 用途 |
|---|---|
| [config.env](config.env) | 机器、模型、镜像版本、副本数、显存预算和端口 |
| [deploy.sh](deploy.sh) | 平台安装、检查、启动、停止、状态、推理验证入口 |
| [manage.py](manage.py) | 参数校验、生成 YAML、两节点文件检查和推理检查 |
| [qwen3-0.6b-16gpu.yaml](qwen3-0.6b-16gpu.yaml) | 默认部署清单，也是参数化生成时的基础清单 |
| [platform-values.yaml](platform-values.yaml) | 使用 Kubernetes 服务发现的 Helm 配置 |

服务器上的脚本目录是 `/data0/dynamo-deploy/`。本脚本接续已经完成的机器准备工作，不执行 `kubeadm reset/init/join`，也不重新安装驱动、GPU Operator 或容器运行时。

前置条件：

- 两个节点 Ready，各可分配 8 张 GPU；Cilium、NVIDIA Container Toolkit、NVIDIA Device Plugin 正常。已有驱动和 Device Plugin 能满足当前部署，不重复安装 GPU Operator。
- a100_1 在验收时具备管理员 kubeconfig、`kubectl`、Helm 3、Python 3 + PyYAML、curl 和 tar。缺 PyYAML 时安装 `apt-get install python3-yaml`。
- 两台节点已缓存 `nvcr.io/nvidia/ai-dynamo/dynamo-frontend:1.6.0` 和 `nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.0`；Operator 镜像为同仓库 `kubernetes-operator:1.6.0`。Planner 镜像已准备，本方案不启动它。
- 两台模型目录完整。脚本自动检查配置、tokenizer 和 safetensors 文件长度/头信息；这项检查不代替文件内容的完整哈希校验。

K8s 使用 containerd，镜像位于 `k8s.io` 命名空间。检查/恢复镜像的命令在**各自节点**执行：

```bash
crictl images
ctr -n k8s.io images import /data0/images/dynamo-frontend-1.6.0-linux-amd64.tar
ctr -n k8s.io images import /data0/images/sglang-runtime-1.6.0-linux-amd64.tar
```

无需重复导入已有镜像。导入失败时先确认 tar 完整、磁盘空间及 containerd 状态。containerd 数据目录为 `/data0/containerd`，镜像 tar 在 `/data0/images`；不要删除运行时数据目录。

## 2. 怎么使用 Dynamo

原集群的管理命令在 a100_1 上运行，使用该集群的 kubeconfig。新集群在实际的控制平面或有权限的管理机运行。

```bash
cd /data0/dynamo-deploy
bash deploy.sh help
```

| 想做什么 | 命令 |
|---|---|
| 安装 Dynamo 平台 | `bash deploy.sh platform` |
| 首次安装平台并启动服务 | `bash deploy.sh up` |
| 检查环境与模型 | `bash deploy.sh check` |
| 预览部署清单 | `bash deploy.sh render` |
| 做预检和服务端 dry run | `bash deploy.sh validate` |
| 平台已安装时启动服务 | `bash deploy.sh start` |
| 查看 DGD、Pod 和 Service | `bash deploy.sh status` |
| 验证 Worker 注册、普通和流式推理 | `bash deploy.sh test` |
| 停止模型服务并等待 Pod 退出 | `bash deploy.sh stop` |

### 启停与验证

首次安装平台或按固定版本更新平台：

```bash
bash deploy.sh platform
```

`platform` 优先使用脚本目录已有的 `dynamo-platform-1.6.0.tgz`，没有时从 NGC 下载，然后执行 Helm 安装/更新。Operator 的 `crd-apply` 初始化流程应用 CRD；脚本继续等待 CRD Established 和 Operator 就绪。运行此命令不会创建模型 Worker。

启动前检查和预览：

```bash
bash deploy.sh check
bash deploy.sh render > /tmp/qwen3-preview.yaml
bash deploy.sh validate
```

`check` 检查节点状态、GPU 资源和镜像缓存，并在两台节点各创建一个临时 CPU Pod，只读检查本地模型文件。检查 Pod 不申请 GPU、不加载模型，结束后自动删除。`validate` 在检查后调用 Kubernetes 服务端 dry run，不创建 Frontend、Worker 或 API Service。`render` 仅输出配置，不访问集群。

平台已安装时启动服务：

```bash
bash deploy.sh start
```

首次安装平台并启动服务也可使用：

```bash
bash deploy.sh up
```

`start` 会进行检查、生成配置、服务端 dry run、提交 DGD/Service，并等待 DGD/Pod Ready，最后检查两节点 API、Frontend 发现的 Worker 数、普通推理和流式推理。首次启动包含内核编译、CUDA Graph 捕获和健康检查；本次实测约两分钟，脚本默认允许 15 分钟。

状态和推理检查：

```bash
bash deploy.sh status
bash deploy.sh test
kubectl get pods -n dynamo-system -o wide
kubectl get dgd,dcd -n dynamo-system
```

停止模型服务并等待 GPU Worker 退出：

```bash
bash deploy.sh stop
```

`stop` 只删除配置中指定的模型 DGD 和 API Service，级联删除其 Frontend/Worker；保留 K8s、Operator/CRD、镜像 tar、模型文件。可以重复执行。两台节点分别用 `nvidia-smi` 确认显存释放。

修改 Worker 参数时，使用 **stop → 修改 config.env → validate → start**。当前 16 张卡已经全部分配时，更新 Worker 配置可能需要新旧副本切换，先停再启可避免因没有空闲 GPU 而等待。运行 `start` 遇到错误会保留业务资源供排查，需要停止时执行 `stop`。

### 参数与显存

编辑 `config.env`，或者用环境变量覆盖某次调用的参数。例如只做两张卡的小规模验证：

```bash
WORKER_REPLICAS=2 bash deploy.sh validate
WORKER_REPLICAS=2 bash deploy.sh start
```

默认仍是 `WORKER_REPLICAS=16`、`MEM_FRACTION_STATIC=0.80`、`MAX_RUNNING_REQUESTS=64`、`CUDA_GRAPH_MAX_BS=64`。`MAX_RUNNING_REQUESTS` 是**每个 Worker** 的并发上限；集群实际可承载并发还受请求长度、KV 容量及性能约束。

`--mem-fraction-static 0.80` 表示权重 + KV Cache 池的静态显存预算，不是模型权重占卡的 80%。本次每张卡实测权重约 1.18 GiB、KV 池约 61.8 GiB，包含 CUDA Graph 等后的总显存约 64.5 GiB。空闲的 KV 槽位也已分配，因此空闲时 `nvidia-smi` 占用依然较高。按要求保留 0.80。

Worker 使用 Triton attention、page size 16；镜像中实际支持的 TP 参数为 `--tp-size`，CUDA Graph 参数为 `--cuda-graph-max-bs-decode`。Frontend 和 Worker 都设置 Hugging Face 离线变量，使用本地文件，无需 HF Token 或在线拉取模型。

本目录参数化只覆盖当前 Qwen3-0.6B、单卡 TP=1 的方案；切换大模型、TP/EP 或 P/D 分离时需要重新设计资源和启动参数。镜像/API 固定支持 1.6.0，其他版本需先核对对应 CRD 和运行时参数。

### 调用 API

服务启动后，下面两个地址任选一个，均由 NodePort 转发到同一个 Frontend：

- `http://10.1.22.129:30080/v1`
- `http://10.1.22.130:30080/v1`

```bash
curl --fail http://10.1.22.129:30080/v1/chat/completions \
  -H 'Content-Type: application/json' \
  -d '{
    "model": "Qwen/Qwen3-0.6B",
    "messages": [{"role":"user","content":"你好"}],
    "max_tokens": 128,
    "chat_template_kwargs": {"enable_thinking": false}
  }'
```

这两个地址提供节点入口冗余；当前 Frontend 是单副本，且 a100_1 是单控制平面，整体架构仍有单点。这里提供的是内网 HTTP 接口。

### 常见问题与排查

把 `POD_NAME` 设为 `kubectl get pods -n dynamo-system` 查到的 Pod 名。`systemctl`、`crictl` 和 `nvidia-smi` 在需要排查的节点上运行。

```bash
kubectl get events -n dynamo-system --sort-by=.lastTimestamp | tail -n 30
kubectl describe pod -n dynamo-system "$POD_NAME"
kubectl logs -n dynamo-system "$POD_NAME" --tail=100
kubectl logs -n dynamo-system "$POD_NAME" --previous --tail=100
systemctl status containerd kubelet
crictl images
nvidia-smi
```

- Pod Pending：确认 GPU 是否被其他工作负载占用、节点是否被 cordon/加 taint，以及 spread 约束能否满足。
- `hostPath type check failed`：检查对应节点的 `/data0/models/qwen3-0.6B`，两台 `/data0` 不是共享目录。
- 镜像下载失败：先确认本地 tag；导入 tar 时必须使用 `ctr -n k8s.io`。曾遇到 NGC 下载连接重置，本地已有完整 tar 时优先导入。
- 启动早期健康检查返回 503：查看权重加载、CUDA Graph 捕获和模型注册进度；不能只以容器 Running 判断推理已就绪。
- DGD 字段错误：本版本 `spec.components` 是列表；`runtimeVersionOverride` 在**各组件**里，不在 DGD 的顶层 `spec`。使用 `validate` 先让已安装的 CRD/webhook 校验。
- `v1/models` 正常但推理失败：继续查看对应 Worker 日志，确认模型加载、GPU 分配、跨节点 Pod 通信和 Worker 注册状态。

## 一句话让 Agent 部署

复制仓库首页的提示词，把模型位置换成实际目录。Agent 用已有的目标机器访问权限，从裸机准备或现有集群检查开始，部署后返回 API 地址、模型名和验收结果。[README 的部署提示词](README.md#一句话部署)

## 验证记录和参考

并发验收使用了不同的随机输入前缀，测试期间 KV 命中为 0。每个 Worker 处理 2 个请求是该次测试的结果，实际分发取决于请求到达顺序、KV 命中和 Worker 负载。

原部署的实际推理验收见第 1 节。之后封装脚本时，另行验证了参数、Helm 渲染、两节点模型文件检查、Kubernetes 服务端 dry run 和停止操作。迁移到新环境时，以该环境执行 `bash deploy.sh test` 的实际结果为准。

参考：

- 原 `Dynamo 部署教程.md`：使用其中 DGD 手动部署、P/D 共置的路线；统一修正示例中的 1.5.0 镜像为 1.6.0，PVC/HF 下载替换为本地只读 hostPath。
- [Dynamo v1.6.0 官方 SGLang 聚合部署与 KV Router 示例](https://github.com/ai-dynamo/dynamo/blob/v1.6.0/examples/backends/sglang/deploy/agg_router.yaml)
- [Dynamo v1.6.0 Platform Helm values](https://github.com/ai-dynamo/dynamo/blob/v1.6.0/deploy/helm/charts/platform/values.yaml)
- [Dynamo Kubernetes Quickstart](https://docs.nvidia.com/dynamo/v1.6.1/kubernetes/getting-started/quickstart)；平台安装命令同时以已下载的 1.6.0 chart 实物核对。
- [SGLang 显存预算、CUDA Graph 和并发调参说明](https://github.com/sgl-project/sglang/blob/main/docs/docs/advanced_features/hyperparameter_tuning.mdx)
- [Kubernetes NodePort Service](https://kubernetes.io/docs/concepts/services-networking/service/#type-nodeport)
