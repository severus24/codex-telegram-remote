.PHONY: test compile check build

test:
	PYTHONPATH=src python3 -m unittest discover -s tests -v

compile:
	PYTHONPATH=src python3 -m compileall -q src tests

check: compile test

build:
	python3 -m pip install build
	python3 -m build
