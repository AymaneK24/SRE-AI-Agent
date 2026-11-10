#!/usr/bin/env bash
# Installs Prometheus with kube-state-metrics and the standard Kubernetes alert rules (kube-prometheus-stack)
# into the cluster kubectl points at, then checks that alerts can be read.
#
#   bash deploy/examples/monitoring/install.sh
#
# Remove it again with: helm uninstall monitoring -n monitoring
set -euo pipefail

here="$(cd "$(dirname "$0")" && pwd)"
release=monitoring
namespace=monitoring
# The latest chart unless you pin one, for example: CHART_VERSION=69.3.0 bash install.sh
chart_version="${CHART_VERSION:-}"

need() { command -v "$1" >/dev/null 2>&1 || { echo "Install $1 first: $2" >&2; exit 1; }; }
need kubectl "https://kubernetes.io/docs/tasks/tools/"
need helm "https://helm.sh/docs/intro/install/"

echo "Cluster: $(kubectl config current-context)"
kubectl get nodes >/dev/null || { echo "kubectl can't reach the cluster. Start it, or choose another context." >&2; exit 1; }

helm repo add prometheus-community https://prometheus-community.github.io/helm-charts >/dev/null 2>&1 || true
helm repo update prometheus-community >/dev/null

echo "Installing kube-prometheus-stack ${chart_version:-(latest)} as '${release}' in namespace '${namespace}'. This takes a few minutes."
helm upgrade --install "$release" prometheus-community/kube-prometheus-stack \
  ${chart_version:+--version "$chart_version"} --namespace "$namespace" --create-namespace \
  --values "$here/values.yaml" --wait --timeout 10m

echo "Waiting for Prometheus to answer"
kubectl -n "$namespace" rollout status statefulset/prometheus-${release}-kube-prometheus-prometheus --timeout=5m

kubectl -n "$namespace" port-forward svc/${release}-kube-prometheus-prometheus 19099:9090 >/dev/null 2>&1 &
forward=$!
trap 'kill $forward 2>/dev/null || true' EXIT
for _ in $(seq 1 20); do
  if curl -fsS http://127.0.0.1:19099/-/ready >/dev/null 2>&1; then break; fi
  sleep 1
done
rules=$(curl -fsS http://127.0.0.1:19099/api/v1/rules | grep -o '"name":"Kube[A-Za-z]*' | sort -u | wc -l || true)
firing=$(curl -fsS http://127.0.0.1:19099/api/v1/alerts | grep -o '"alertname":"[A-Za-z]*' | sort -u | sed 's/"alertname":"//' | tr '\n' ' ' || true)

cat <<EOF

Done. Prometheus is running with ${rules} Kubernetes alert rules.
Alerts firing right now: ${firing:-none yet. Rules need a few minutes, and most wait 10 to 15 minutes before firing}

Connect it in Root Cause Console, Connections > Metric sources > Prometheus:
  - Console in Docker on this machine (Docker Desktop):  host host.docker.internal, port 30090, http
  - Console inside this cluster:  host ${release}-kube-prometheus-prometheus.${namespace}.svc.cluster.local, port 9090, http
  - Anywhere else: this machine's IP address, port 30090
EOF
