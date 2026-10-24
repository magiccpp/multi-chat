# Works with Docker or nerdctl:  make up COMPOSE="nerdctl compose"
COMPOSE ?= docker compose
PY ?= python3

.PHONY: help init up down logs build test test-local dev-db

help:
	@echo "make init      create .env (random secrets) and config.yaml from the examples"
	@echo "make up        build and start the server stack"
	@echo "make down      stop it"
	@echo "make logs      follow server logs"
	@echo "make test      run the server test suite in containers"
	@echo "make test-local  run server + client tests against DATABASE_URL"

init:
	@test -f .env || { sed -e "s/^MULTICHAT_API_TOKEN=.*/MULTICHAT_API_TOKEN=$$(openssl rand -hex 32)/" \
	  -e "s/^POSTGRES_PASSWORD=.*/POSTGRES_PASSWORD=$$(openssl rand -hex 16)/" .env.example > .env; \
	  chmod 600 .env; echo "created .env"; }
	@test -f config.yaml || { cp config.example.yaml config.yaml; echo "created config.yaml"; }
	@grep '^MULTICHAT_API_TOKEN=' .env

up: init
	$(COMPOSE) up -d --build

down:
	$(COMPOSE) down

logs:
	$(COMPOSE) logs -f server

build:
	$(COMPOSE) build

test:
	$(COMPOSE) -f compose.test.yaml run --rm tests
	$(COMPOSE) -f compose.test.yaml down -v

test-local:
	cd server && $(PY) -m pytest -q
	cd clients/core && $(PY) -m pytest -q
