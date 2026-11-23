# k8s-dynamo

在 GPU 服务器上部署 Kubernetes 和 Dynamo，并启动模型推理服务。

## 一句话部署

把下面这句话发给 Agent。`<模型位置>` 填实际模型目录或 Hugging Face 仓库 ID。`<存放位置>` 可选填，指部署文件和镜像归档在目标机上的存放目录。

```text
请按 https://github.com/Ack508/k8s-dynamo 的《可迁移说明.md》，在你已有权限的目标机器上部署 Kubernetes 和 Dynamo 并启动推理服务，模型位置是 <模型位置>，部署文件和镜像归档的存放位置是 <存放位置>，完成后请验证推理并给我 API 地址和模型名。
```

例如，模型位置可以是 `/mnt/ssd/models/qwen3-0.6B`，存放位置可以是 `/mnt/ssd/dynamo`。如果没有指定存放位置，删掉提示词里的对应分句，由 Agent 根据目标机器的磁盘和空间选择。目标机器的访问权限需事先配置好。

[我们的部署过程与使用命令](Dynamo部署与使用交付说明.md) · [裸机部署可迁移说明](可迁移说明.md) · [启停脚本](deploy.sh) · [部署参数](config.env)
