.PHONY: cluster down build load chart-sync deploy up test lint e2e bench

KIND_CLUSTER := demo-orchestrator
CHART := charts/demo-orchestrator
# Extra helm flags, e.g. make deploy HELM_ARGS="--set sweeper.suspend=true"
HELM_ARGS ?=
INGRESS_NGINX_MANIFEST := https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/kind/deploy.yaml

cluster:
	kind create cluster --name $(KIND_CLUSTER) --config deploy/kind/cluster.yaml
	kubectl apply -f $(INGRESS_NGINX_MANIFEST)
	kubectl --namespace ingress-nginx rollout status deployment/ingress-nginx-controller --timeout=180s

down:
	kind delete cluster --name $(KIND_CLUSTER)

build:
	docker build -f docker/orchestrator.Dockerfile -t demo-orchestrator:dev .
	docker build -f docker/crewline.Dockerfile -t crewline:dev .

load:
	kind load docker-image demo-orchestrator:dev --name $(KIND_CLUSTER)
	kind load docker-image crewline:dev --name $(KIND_CLUSTER)

# The chart installs the CRD from crds/ and builds the personas ConfigMap from personas/.
chart-sync:
	rm -rf $(CHART)/crds $(CHART)/personas && mkdir -p $(CHART)/crds
	cp deploy/crd/*.yaml $(CHART)/crds/ && cp -R personas $(CHART)/personas

deploy: chart-sync
	helm upgrade --install demo-orchestrator $(CHART) --namespace demo-orchestrator --create-namespace --wait --timeout 5m $(HELM_ARGS)

up: cluster build load deploy

test:
	uv run pytest tests/unit -q

lint:
	uv run ruff check .
	uv run ruff format --check .
	uv run mypy src

e2e:
	uv run pytest -m integration -v

bench:
	uv run democtl bench --persona healthcare --n 10 --ttl 2m
