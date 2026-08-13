.PHONY: setup check calls one report scenarios clean

setup:
	python -m venv .venv && .venv/bin/pip install -U pip && .venv/bin/pip install -r requirements.txt
	@echo "now: cp .env.example .env && edit it"

check:
	.venv/bin/python -m patientsim check

calls:
	.venv/bin/python -m patientsim run --all

one:
	.venv/bin/python -m patientsim run -s $(S)

report:
	.venv/bin/python -m patientsim report

scenarios:
	.venv/bin/python -m patientsim scenarios

clean:
	rm -f artifacts/*.wav
