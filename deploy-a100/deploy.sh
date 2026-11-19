#!/usr/bin/env bash
set -euo pipefail
DEPLOY_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
source "${DYNAMO_CONFIG:-${DEPLOY_DIR}/config.env}"
export DEPLOY_DIR

usage() {
  cat <<'EOF'
Usage: bash deploy.sh COMMAND
  platform  Install/upgrade Dynamo Platform 1.6.0 and wait for Operator/CRDs.
  check     Check nodes, cached images, GPU capacity and both local models.
  render    Print the DGD + NodePort YAML for current config; create no resources.
  validate  Run cluster/model checks and Kubernetes server-side dry run.
  start     Validate, start 16 single-GPU workers, wait and check inference.
  up        Run platform, then start.
  stop      Delete this model's DGD/API Service and wait for its Pods to exit.
  status    Show this model's DGD, Pods and Services.
  test      Test worker discovery, normal completion and streaming completion.
  help      Print this help. Running without a command prints help too.

Edit config.env, or override one setting, e.g. WORKER_REPLICAS=2 bash deploy.sh start.
Run on a100_1 with an administrative kubeconfig. stop keeps the platform and files.
EOF
}

require() {
  local tool
  for tool in "$@"; do
    command -v "$tool" >/dev/null || { echo "Missing command: $tool" >&2; exit 1; }
  done
}

platform() {
  require helm kubectl curl tar
  local chart="${DEPLOY_DIR}/dynamo-platform-${DYNAMO_VERSION}.tgz"
  if [[ ! -s "$chart" ]]; then
    curl --fail --location --retry 3 --connect-timeout 15 \
      "https://helm.ngc.nvidia.com/nvidia/ai-dynamo/charts/dynamo-platform-${DYNAMO_VERSION}.tgz" \
      --output "${chart}.part"
    tar -tzf "${chart}.part" >/dev/null
    mv -- "${chart}.part" "$chart"
  fi
  helm upgrade --install dynamo-platform "$chart" \
    --namespace "$NAMESPACE" --create-namespace \
    --values "${DEPLOY_DIR}/platform-values.yaml" --wait --timeout=10m
  wait_platform
}

wait_platform() {
  kubectl wait --for=condition=Established \
    crd/dynamographdeployments.nvidia.com \
    crd/dynamographdeploymentrequests.nvidia.com --timeout=120s
  kubectl rollout status deployment/dynamo-platform-dynamo-operator-controller-manager \
    -n "$NAMESPACE" --timeout=180s
}

status() {
  kubectl get dgd "$DGD_NAME" -n "$NAMESPACE" --ignore-not-found
  kubectl get pods -n "$NAMESPACE" -l "app.kubernetes.io/instance=${DGD_NAME}" -o wide
  kubectl get svc "$API_SERVICE" "${DGD_NAME}-frontend" -n "$NAMESPACE" --ignore-not-found
}

stop() {
  kubectl delete "dgd/${DGD_NAME}" "service/${API_SERVICE}" -n "$NAMESPACE" \
    --ignore-not-found --cascade=foreground --wait=true --timeout=180s
  kubectl wait --for=delete pod -n "$NAMESPACE" \
    -l "app.kubernetes.io/instance=${DGD_NAME}" --timeout=180s
  echo 'Model service stopped; Operator, Kubernetes, images and models are retained.'
}

validate_or_start() {
  wait_platform
  python3 "${DEPLOY_DIR}/manage.py" check
  manifest="$(mktemp "${DEPLOY_DIR}/.dgd.XXXXXX.yaml")"
  trap 'rm -f -- "${manifest:-}"' EXIT
  python3 "${DEPLOY_DIR}/manage.py" render >"$manifest"
  kubectl apply --dry-run=server -f "$manifest"
  if [[ "$1" == start ]]; then
    # At full GPU capacity, use stop -> start when changing worker settings.
    kubectl apply -f "$manifest"
    if ! kubectl wait --for=condition=Ready "dgd/${DGD_NAME}" \
      -n "$NAMESPACE" --timeout="$WAIT_TIMEOUT"; then
      status
      kubectl get events -n "$NAMESPACE" --sort-by=.lastTimestamp | tail -n 25
      echo 'Startup failed; inspect kubectl logs before retrying.' >&2
      return 1
    fi
    kubectl wait --for=condition=Ready pod -n "$NAMESPACE" \
      -l "app.kubernetes.io/instance=${DGD_NAME}" --timeout=120s
    python3 "${DEPLOY_DIR}/manage.py" test
    status
  fi
}

action="${1:-help}"
case "$action" in
  help|-h|--help) usage; exit 0 ;;
  platform|check|render|validate|start|up|stop|status|test) ;;
  *) usage >&2; exit 2 ;;
esac
require python3
python3 -c 'import yaml' 2>/dev/null || {
  echo 'Missing PyYAML: apt-get install python3-yaml' >&2; exit 1;
}
# Validate config before any changes, including Helm installation/deletion.
python3 "${DEPLOY_DIR}/manage.py" config
[[ "$action" == render ]] || require kubectl
case "$action" in
  platform) platform ;;
  check) python3 "${DEPLOY_DIR}/manage.py" check ;;
  render) python3 "${DEPLOY_DIR}/manage.py" render ;;
  validate) validate_or_start validate ;;
  start) validate_or_start start ;;
  up) platform; validate_or_start start ;;
  stop) stop ;;
  status) status ;;
  test) python3 "${DEPLOY_DIR}/manage.py" test ;;
esac
