# k8s-dynamo

在 GPU 服务器上部署 Kubernetes 和 Dynamo，并启动模型推理服务。

## 一句话部署

把下面这句话发给 Agent，把 `<模型位置>` 换成实际模型目录或 Hugging Face 仓库 ID。

```text
请按 https://github.com/Ack508/k8s-dynamo 的《可迁移说明.md》，在你已有权限的目标机器上部署 Kubernetes 和 Dynamo 并启动推理服务，模型位置是 <模型位置>，完成后请验证推理并给我 API 地址和模型名。
```

例如，模型位置可以是 `/data0/models/qwen3-0.6B`。Agent 根据目标机器实际情况检查节点、GPU、存储、镜像和模型文件。目标机器的访问权限需事先配置好。

[我们的部署过程与使用命令](Dynamo部署与使用交付说明.md) · [裸机部署可迁移说明](可迁移说明.md) · [启停脚本](deploy.sh) · [部署参数](config.env)
