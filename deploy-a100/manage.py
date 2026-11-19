#!/usr/bin/env python3
"""Configuration, preflight and smoke checks for the two A100 nodes."""
import concurrent.futures
import ipaddress
import json
import math
import os
from pathlib import Path
import re
import secrets
import subprocess
import sys
import time
import urllib.request

import yaml


def run(*args, data=None):
    return subprocess.check_output(args, input=data, text=True, timeout=60)


def kube(*args):
    return json.loads(run("kubectl", *args, "-o", "json"))


def config():
    keys = ("DYNAMO_VERSION NAMESPACE DGD_NAME API_SERVICE NODE_NAMES NODE_IPS "
            "NODE_PORT MODEL_HOST_PATH SERVED_MODEL WORKER_REPLICAS "
            "MEM_FRACTION_STATIC MAX_RUNNING_REQUESTS CUDA_GRAPH_MAX_BS WAIT_TIMEOUT").split()
    c = {key: os.environ[key] for key in keys}
    if c["DYNAMO_VERSION"] != "1.6.0":
        raise ValueError("This recipe is validated for Dynamo 1.6.0 only")
    for key in ("NAMESPACE", "DGD_NAME", "API_SERVICE"):
        if not re.fullmatch(r"[a-z0-9](?:[a-z0-9-]*[a-z0-9])?", c[key]) or len(c[key]) > 40:
            raise ValueError(f"{key}: use a lowercase DNS label of at most 40 characters")
    c["nodes"] = c["NODE_NAMES"].split(",")
    c["ips"] = c["NODE_IPS"].split(",")
    if not c["nodes"] or len(set(c["nodes"])) != len(c["nodes"]):
        raise ValueError("NODE_NAMES must contain unique node names")
    if len(c["nodes"]) != len(c["ips"]):
        raise ValueError("NODE_NAMES and NODE_IPS must have equal lengths")
    for address in c["ips"]:
        if ipaddress.ip_address(address).version != 4:
            raise ValueError("This recipe expects IPv4 node addresses")
    for key in ("NODE_PORT", "WORKER_REPLICAS", "MAX_RUNNING_REQUESTS", "CUDA_GRAPH_MAX_BS"):
        c[key] = int(c[key])
        if c[key] < 1:
            raise ValueError(f"{key} must be positive")
    if not 30000 <= c["NODE_PORT"] <= 32767:
        raise ValueError("NODE_PORT must be within the cluster's default 30000-32767 range")
    fraction = float(c["MEM_FRACTION_STATIC"])
    if not math.isfinite(fraction) or not 0 < fraction < 1:
        raise ValueError("MEM_FRACTION_STATIC must be between 0 and 1, exclusive")
    if not c["MODEL_HOST_PATH"].startswith("/") or not c["SERVED_MODEL"].strip():
        raise ValueError("Use an absolute MODEL_HOST_PATH and nonempty SERVED_MODEL")
    if not re.fullmatch(r"[1-9][0-9]*[smh]", c["WAIT_TIMEOUT"]):
        raise ValueError("WAIT_TIMEOUT must look like 900s, 15m or 1h")
    return c


def documents(c):
    path = Path(__file__).with_name("qwen3-0.6b-16gpu.yaml")
    dgd, service = list(yaml.safe_load_all(path.read_text(encoding="utf-8")))
    for obj, name in ((dgd, c["DGD_NAME"]), (service, c["API_SERVICE"])):
        obj["metadata"].update(name=name, namespace=c["NAMESPACE"])
    frontend, worker = dgd["spec"]["components"]
    worker["replicas"] = c["WORKER_REPLICAS"]
    for component, image in ((frontend, "dynamo-frontend"), (worker, "sglang-runtime")):
        component["runtimeVersionOverride"] = c["DYNAMO_VERSION"]
        pod = component["podTemplate"]
        pod["metadata"]["labels"]["app.kubernetes.io/instance"] = c["DGD_NAME"]
        pod["spec"]["affinity"] = {"nodeAffinity": {
            "requiredDuringSchedulingIgnoredDuringExecution": {"nodeSelectorTerms": [{
                "matchExpressions": [{"key": "kubernetes.io/hostname", "operator": "In",
                                      "values": list(c["nodes"])}]}]}}}
        pod["spec"]["containers"][0]["image"] = f"nvcr.io/nvidia/ai-dynamo/{image}:{c['DYNAMO_VERSION']}"
        pod["spec"]["volumes"][0]["hostPath"]["path"] = c["MODEL_HOST_PATH"]
    args = worker["podTemplate"]["spec"]["containers"][0]["args"]
    for flag, value in {"--served-model-name": c["SERVED_MODEL"],
                        "--mem-fraction-static": c["MEM_FRACTION_STATIC"],
                        "--max-running-requests": c["MAX_RUNNING_REQUESTS"],
                        "--cuda-graph-max-bs-decode": c["CUDA_GRAPH_MAX_BS"]}.items():
        args[args.index(flag) + 1] = str(value)
    spread = worker["podTemplate"]["spec"]["topologySpreadConstraints"][0]
    spread["labelSelector"]["matchLabels"]["app.kubernetes.io/instance"] = c["DGD_NAME"]
    service["spec"]["selector"]["app.kubernetes.io/instance"] = c["DGD_NAME"]
    service["spec"]["ports"][0]["nodePort"] = c["NODE_PORT"]
    return dgd, service


MODEL_CHECK = r'''
import json, pathlib, struct
p = pathlib.Path('/models/qwen3-0.6B')
for name in ['config.json', 'tokenizer.json', 'tokenizer_config.json', 'model.safetensors']:
    assert (p/name).is_file() and (p/name).stat().st_size > 0, 'Missing/empty '+name
config = json.loads((p/'config.json').read_text())
assert config['model_type'] == 'qwen3', config.get('model_type')
w = p/'model.safetensors'
with w.open('rb') as f:
    size = struct.unpack('<Q', f.read(8))[0]
    assert size < 100_000_000, 'Invalid safetensors header'
    header = json.loads(f.read(size))
end = max(x['data_offsets'][1] for k,x in header.items() if k != '__metadata__')
assert w.stat().st_size == 8+size+end, 'Incomplete model.safetensors'
print('OK: local Qwen3 model and safetensors size/header; bytes='+str(w.stat().st_size))
'''


def model_check(node, c):
    name = "qwen3-file-check-" + secrets.token_hex(4)
    pod = {"apiVersion": "v1", "kind": "Pod", "metadata": {
        "name": name, "namespace": c["NAMESPACE"], "labels": {"app": "qwen3-file-check"}},
        "spec": {"nodeName": node, "restartPolicy": "Never", "activeDeadlineSeconds": 110,
                 "terminationGracePeriodSeconds": 2, "containers": [{"name": "check",
                 "image": f"nvcr.io/nvidia/ai-dynamo/sglang-runtime:{c['DYNAMO_VERSION']}",
                 "imagePullPolicy": "Never", "command": ["python3", "-c", MODEL_CHECK],
                 "resources": {"requests": {"cpu": "100m", "memory": "128Mi"},
                               "limits": {"memory": "256Mi"}},
                 "volumeMounts": [{"name": "model", "mountPath": "/models/qwen3-0.6B", "readOnly": True}]}],
                 "volumes": [{"name": "model", "hostPath": {"path": c["MODEL_HOST_PATH"], "type": "Directory"}}]}}
    created = False
    try:
        run("kubectl", "create", "-f", "-", data=json.dumps(pod))
        created = True
        deadline = time.monotonic() + 120
        while time.monotonic() < deadline:
            phase = kube("get", "pod", name, "-n", c["NAMESPACE"])["status"]["phase"]
            if phase in ("Succeeded", "Failed"):
                log = run("kubectl", "logs", name, "-n", c["NAMESPACE"])
                if phase == "Failed":
                    raise RuntimeError(f"{node}: model check failed\n{log}")
                print(f"{node}: {log.strip()}", flush=True)
                return
            time.sleep(1)
        raise RuntimeError(f"{node}: file-check Pod did not finish; inspect node/mount events")
    finally:
        if created:
            run("kubectl", "delete", "pod", name, "-n", c["NAMESPACE"], "--ignore-not-found", "--timeout=30s")


def check(c):
    free = 0
    pods = kube("get", "pods", "-A")["items"]
    for node in c["nodes"]:
        obj = kube("get", "node", node)
        if not any(x["type"] == "Ready" and x["status"] == "True" for x in obj["status"]["conditions"]):
            raise RuntimeError(f"Node {node} is not Ready")
        labels = obj["metadata"].get("labels", {})
        if labels.get("kubernetes.io/hostname") != node:
            raise RuntimeError(f"{node}: hostname label differs from configured node name")
        if obj["spec"].get("unschedulable") or any(
                t.get("effect") in ("NoSchedule", "NoExecute") for t in obj["spec"].get("taints", [])):
            raise RuntimeError(f"{node}: cordoned or has an unsupported scheduling taint")
        capacity = int(obj["status"]["allocatable"].get("nvidia.com/gpu", "0"))
        other = 0
        for p in pods:
            own = (p["metadata"]["namespace"] == c["NAMESPACE"] and
                   p["metadata"].get("labels", {}).get("app.kubernetes.io/instance") == c["DGD_NAME"])
            if p["spec"].get("nodeName") == node and p["status"]["phase"] not in ("Succeeded", "Failed") and not own:
                regular = sum(int(x.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0))
                              for x in p["spec"]["containers"])
                initial = max((int(x.get("resources", {}).get("requests", {}).get("nvidia.com/gpu", 0))
                               for x in p["spec"].get("initContainers", [])), default=0)
                other += max(regular, initial)
        free += capacity - other
        names = {name for image in obj["status"].get("images", []) for name in image["names"]}
        for image in ("dynamo-frontend", "sglang-runtime"):
            reference = f"nvcr.io/nvidia/ai-dynamo/{image}:{c['DYNAMO_VERSION']}"
            if reference not in names:
                raise RuntimeError(f"{node}: image tag missing from node cache: {reference}")
        print(f"{node}: Ready; GPUs={capacity}; allocated to other workloads={other}; images cached", flush=True)
    if free < c["WORKER_REPLICAS"]:
        raise RuntimeError(f"Requested {c['WORKER_REPLICAS']} GPUs; only {free} available including this deployment")
    print("Checking model files with temporary CPU Pods; no GPU requests or model loading.", flush=True)
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(c["nodes"])) as pool:
        list(pool.map(lambda node: model_check(node, c), c["nodes"]))
    print("Preflight passed.", flush=True)


def smoke(c):
    base = f"http://{c['ips'][0]}:{c['NODE_PORT']}"
    for address in c["ips"]:
        url = f"http://{address}:{c['NODE_PORT']}/v1/models"
        with urllib.request.urlopen(url, timeout=10) as response:
            models = json.load(response)
        if c["SERVED_MODEL"] not in [x["id"] for x in models["data"]]:
            raise RuntimeError(f"Served model missing at {url}")
        print(f"OK: {url}")
    with urllib.request.urlopen(base + "/health", timeout=10) as response:
        health = json.load(response)
    workers = [x for x in health.get("instances", []) if x.get("endpoint") == "generate"]
    if health.get("status") != "healthy" or len(workers) != c["WORKER_REPLICAS"]:
        raise RuntimeError(f"Frontend health/registration mismatch: {len(workers)} generate workers")
    print(f"OK: Frontend discovered {len(workers)} workers")
    for stream in (False, True):
        payload = {"model": c["SERVED_MODEL"], "messages": [{"role": "user", "content": "Say hello briefly."}],
                   "max_tokens": 32, "temperature": 0, "stream": stream,
                   "chat_template_kwargs": {"enable_thinking": False}}
        request = urllib.request.Request(base + "/v1/chat/completions", data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=60) as response:
            text = response.read().decode()
        if stream:
            events = [json.loads(line[6:]) for line in text.splitlines()
                      if line.startswith("data: ") and line[6:] != "[DONE]"]
            if "data: [DONE]" not in text or not any(
                    (e.get("choices") or [{}])[0].get("delta", {}).get("content") for e in events):
                raise RuntimeError("Incomplete or empty stream")
        elif not json.loads(text)["choices"][0]["message"].get("content"):
            raise RuntimeError("Empty model response")
        print(f"OK: {'streaming' if stream else 'normal'} completion")


def main():
    c = config()
    action = sys.argv[1]
    if action == "render":
        print(yaml.safe_dump_all(documents(c), sort_keys=False, allow_unicode=True), end="")
    elif action == "check":
        check(c)
    elif action == "test":
        smoke(c)
    elif action != "config":
        raise ValueError(f"Unknown action: {action}")


if __name__ == "__main__":
    try:
        main()
    except (ValueError, KeyError, RuntimeError, OSError, subprocess.SubprocessError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        sys.exit(1)
