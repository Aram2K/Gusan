Write-Host "Setting up Gusan..." -ForegroundColor Green

if (-Not (Test-Path ".\venv")) { python -m venv venv }

& .\venv\Scripts\Activate.ps1

pip install -r requirements.txt --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu

Write-Host "Done! Double-click run.bat to start." -ForegroundColor Green
