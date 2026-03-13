.PHONY: guardrails lint typecheck test audit check

guardrails:
	python check_repo_guardrails.py

lint:
	ruff check chatter/ tests/

typecheck:
	mypy chatter/ --ignore-missing-imports

test:
	pytest tests/ -v

audit:
	pip-audit

check: guardrails lint typecheck test audit