.PHONY: init install test clean clean-all

init:
	uv sync
	uv run pre-commit install

install:
	uv tool install --editable --force .
	rfp --install-completion
	@echo
	@echo "Shell completion installed. Existing terminals won't see it until you"
	@echo "run 'source ~/.bashrc' (or the equivalent for your shell) or open a new one."

test:
	uv run pytest

clean:
	find . -type d -name "__pycache__" -exec rm -rf {} +
	find . -type d -name "*.egg-info" -exec rm -rf {} +
	rm -rf .pytest_cache .ruff_cache build dist

clean-all: clean
	rm -rf .venv
