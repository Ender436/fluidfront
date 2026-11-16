#!/usr/bin/env bash
# FluidFront launcher - run ./start.sh (or double-click) to start the app.
# First run also sets up the Python environment and fetches the solver
# engine; after that it just starts the server and prints the URL.
set -e
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
  echo "First run: creating the Python environment (one time only)..."
  python3 -m venv .venv
  .venv/bin/pip install -q -e .
fi

if [ ! -d engine/FluidX3D ]; then
  echo "First run: fetching the FluidX3D engine (one time only)..."
  git clone --branch v3.8 --depth 1 \
    https://github.com/ProjectPhysX/FluidX3D.git engine/FluidX3D \
    || echo "could not clone the engine - run 'make engine' before your first simulation"
fi

echo "FluidFront is starting - open http://localhost:8123  (Ctrl+C to stop)"
exec .venv/bin/python -m uvicorn fluidfront.server.app:app --port 8123
