# 两台 A100 的 Dynamo 部署脚本

本目录基于原《Dynamo 部署教程.md》、Dynamo **v1.6.0** 官方示例，以及 2026-11-19 在两台机器上实际跑通的部署过程整理。脚本在 **a100_1** 上执行，通过 Kubernetes 管理两台节点，不要求节点之间配置 SSH 免密。

当前模型业务已停止；Kubernetes、Dynamo Operator/CRD、镜像和本地模型保留。运行 `start` / `up` 才会启动模型服务。

## 部署配置

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

## 文件与前置条件

| 文件 | 用途 |
|---|---|
| `config.env` | 机器、模型、镜像版本、副本数、显存预算和端口 |
| `deploy.sh` | 平台安装、检查、启动、停止、状态、推理验证入口 |
| `manage.py` | 参数校验、生成 YAML、两节点文件检查和推理检查 |
| `qwen3-0.6b-16gpu.yaml` | 默认部署清单，也是参数化生成时的基础清单 |
| `platform-values.yaml` | 平台 Helm 配置：Kubernetes 发现、不启用可选的 etcd/NATS/Grove 等组件 |

服务器上的脚本目录是 `/data0/dynamo-deploy/`。本脚本接续已经完成的机器准备工作，不执行 `kubeadm reset/init/join`，也不重新安装驱动、GPU Operator 或容器运行时。

前置条件：

- 两个节点 Ready，各可分配 8 张 GPU；Cilium、NVIDIA Container Toolkit、NVIDIA Device Plugin 正常。已有驱动和 Device Plugin 能满足当前部署，不重复安装 GPU Operator。
- a100_1 有管理员 kubeconfig、`kubectl`、Helm 3、Python 3 + PyYAML、curl、tar。当前均已具备；缺 PyYAML 时安装 `apt-get install python3-yaml`。
- 两台节点已缓存 `nvcr.io/nvidia/ai-dynamo/dynamo-frontend:1.6.0` 和 `nvcr.io/nvidia/ai-dynamo/sglang-runtime:1.6.0`；Operator 镜像为同仓库 `kubernetes-operator:1.6.0`。Planner 镜像已准备，本方案不启动它。
- 两台模型目录完整。脚本自动检查配置、tokenizer 和 safetensors 文件长度/头信息；这项检查不代替文件内容的完整哈希校验。

K8s 使用 containerd，镜像位于 `k8s.io` 命名空间。检查/恢复镜像的命令在**各自节点**执行：

```bash
crictl images
ctr -n k8s.io images import /data0/images/dynamo-frontend-1.6.0-linux-amd64.tar
ctr -n k8s.io images import /data0/images/sglang-runtime-1.6.0-linux-amd64.tar
```

无需重复导入已有镜像。导入失败时先确认 tar 完整、磁盘空间及 containerd 状态。containerd 数据目录为 `/data0/containerd`，镜像 tar 在 `/data0/images`；不要删除运行时数据目录。

## 使用步骤

在 a100_1 上：

```bash
cd /data0/dynamo-deploy
bash deploy.sh help
```

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

## 参数与显存

编辑 `config.env`，或者用环境变量覆盖某次调用的参数。例如只做两张卡的小规模验证：

```bash
WORKER_REPLICAS=2 bash deploy.sh validate
WORKER_REPLICAS=2 bash deploy.sh start
```

默认仍是 `WORKER_REPLICAS=16`、`MEM_FRACTION_STATIC=0.80`、`MAX_RUNNING_REQUESTS=64`、`CUDA_GRAPH_MAX_BS=64`。`MAX_RUNNING_REQUESTS` 是**每个 Worker** 的并发上限；集群实际可承载并发还受请求长度、KV 容量及性能约束。

`--mem-fraction-static 0.80` 表示权重 + KV Cache 池的静态显存预算，不是模型权重占卡的 80%。本次每张卡实测权重约 1.18 GiB、KV 池约 61.8 GiB，包含 CUDA Graph 等后的总显存约 64.5 GiB。空闲的 KV 槽位也已分配，因此空闲时 `nvidia-smi` 占用依然较高。按要求保留 0.80。

Worker 使用 Triton attention、page size 16；镜像中实际支持的 TP 参数为 `--tp-size`，CUDA Graph 参数为 `--cuda-graph-max-bs-decode`。Frontend 和 Worker 都设置 Hugging Face 离线变量，使用本地文件，无需 HF Token 或在线拉取模型。

本目录参数化只覆盖当前 Qwen3-0.6B、单卡 TP=1 的方案；切换大模型、TP/EP 或 P/D 分离时需要重新设计资源和启动参数。镜像/API 固定支持 1.6.0，其他版本需先核对对应 CRD 和运行时参数。

## 调用 API

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

## 常见问题与排查

```bash
kubectl get events -n dynamo-system --sort-by=.lastTimestamp | tail -n 30
kubectl describe pod -n dynamo-system <pod-name>
kubectl logs -n dynamo-system <pod-name> --tail=100
kubectl logs -n dynamo-system <pod-name> --previous --tail=100
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

## 验证记录和参考

2026-11-19 原始部署验证：1 个 Frontend、16 个 Worker 全部 Ready；每台 8 个 Worker；32 个并发请求全部成功，Frontend 日志显示每个 Worker 各处理 2 个；两节点 NodePort 和流式输出均验证成功。测试使用不同随机输入前缀，KV 命中为 0，此次结果主要体现负载均衡，不代表固定每卡分两个请求。

整理脚本时保持服务停止。脚本检查、服务端 dry run、Helm 模板渲染与停止操作单独验证，不在本次整理过程中重新启动 16 卡推理。历史推理通过不等于脚本在所有新机器和新配置下都已完成实际推理测试。

参考：

- 原 `Dynamo 部署教程.md`：使用其中 DGD 手动部署、P/D 共置的路线；统一修正示例中的 1.5.0 镜像为 1.6.0，PVC/HF 下载替换为本地只读 hostPath。
- [Dynamo v1.6.0 官方 SGLang 聚合部署与 KV Router 示例](https://github.com/ai-dynamo/dynamo/blob/v1.6.0/examples/backends/sglang/deploy/agg_router.yaml)
- [Dynamo v1.6.0 Platform Helm values](https://github.com/ai-dynamo/dynamo/blob/v1.6.0/deploy/helm/charts/platform/values.yaml)
- [Dynamo Kubernetes Quickstart](https://docs.nvidia.com/dynamo/v1.6.1/kubernetes/getting-started/quickstart)；平台安装命令同时以已下载的 1.6.0 chart 实物核对。
- [SGLang 显存预算、CUDA Graph 和并发调参说明](https://github.com/sgl-project/sglang/blob/main/docs/docs/advanced_features/hyperparameter_tuning.mdx)
- [Kubernetes NodePort Service](https://kubernetes.io/docs/concepts/services-networking/service/#type-nodeport)
