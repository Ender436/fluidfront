@echo off
rem FluidFront launcher - double-click to start the app.
rem First run also sets up the Python environment and fetches the solver
rem engine; after that it just starts the server. The engine itself gets
rem compiled automatically on your first simulation run (~1-2 min, needs
rem the Visual Studio C++ build tools once - see README).
cd /d "%~dp0"

if not exist .venv\Scripts\python.exe (
  echo First run: creating the Python environment ^(one time only^)...
  py -3 -m venv .venv || (echo Python 3 not found - install it from python.org & pause & exit /b 1)
  call .venv\Scripts\pip install -q -e . || (echo dependency install failed & pause & exit /b 1)
)

if not exist engine\FluidX3D (
  echo First run: fetching the FluidX3D engine ^(one time only^)...
  git clone --branch v3.8 --depth 1 https://github.com/ProjectPhysX/FluidX3D.git engine\FluidX3D || echo Could not clone the engine - install Git for Windows, then run this again.
)

echo FluidFront is starting - open http://localhost:8123  (close this window to stop)
.venv\Scripts\python -m uvicorn fluidfront.server.app:app --port 8123
pause
