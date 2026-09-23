.PHONY: cluster down build load deploy up test lint e2e bench

KIND_CLUSTER := demo-orchestrator
INGRESS_NGINX_MANIFEST := https://raw.githubusercontent.com/kubernetes/ingress-nginx/controller-v1.15.1/deploy/static/provider/kind/deploy.yaml

cluster:
	kind create cluster --name $(KIND_CLUSTER) --config deploy/kind/cluster.yaml
	kubectl apply -f $(INGRESS_NGINX_MANIFEST)
	kubectl wait --namespace ingress-nginx --for=condition=ready pod --selector=app.kubernetes.io/component=controller --timeout=120s

down:
	kind delete cluster --name $(KIND_CLUSTER)

build:
	docker build -f docker/orchestrator.Dockerfile -t demo-orchestrator:dev .
	docker build -f docker/crewline.Dockerfile -t crewline:dev .

load:
	kind load docker-image demo-orchestrator:dev --name $(KIND_CLUSTER)
	kind load docker-image crewline:dev --name $(KIND_CLUSTER)

deploy:
	helm upgrade --install demo-orchestrator charts/demo-orchestrator --namespace demo-orchestrator --create-namespace

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
