PY := .venv/bin/python

.PHONY: engine setup serve bench check clean

engine:
	@test -d engine/FluidX3D || git clone --branch v3.8 --depth 1 \
	  https://github.com/ProjectPhysX/FluidX3D.git engine/FluidX3D
	cd engine/FluidX3D && make Linux -j$$(nproc)

setup:
	python3 -m venv .venv
	.venv/bin/pip install -e .

serve:
	$(PY) -m uvicorn flowbench.server.app:app --port 8123

bench:
	cd engine/FluidX3D && ./bin/FluidX3D

check:
	$(PY) -m flowbench.validate.runner

clean:
	cd engine/FluidX3D && make clean
