.PHONY: demo test evals serve install install-pip

install:  ## Install the locked set with uv (primary workflow)
	uv sync --extra dev

install-pip:  ## Fallback install: pip with the pinned/frozen set
	# The frozen set first, then the package itself with --no-deps:
	# pip refuses an editable install under a hashed constraints
	# file ("no single file to hash"), so the two never mix.
	pip install -r constraints.txt
	pip install --no-deps -e .

demo:  ## Traced demo: the whole synthetic AP world, end to end (offline)
	uv run reducto-ap-agent-demo

test:  ## Run the pytest suite
	uv run pytest -q

evals:  ## Run the golden-dataset evals (gates: fraud = 0 misses, match accuracy)
	uv run python evals/run_evals.py

serve:  ## Serve the API + web UI on http://localhost:8000
	uv run uvicorn reducto_ap_agent.api:app --port 8000
