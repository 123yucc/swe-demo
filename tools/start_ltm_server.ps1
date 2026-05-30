# Start the experience_server (LTM) on the host so Docker containers can reach it
# via host.docker.internal:9030.
#
# Usage: .\tools\start_ltm_server.ps1
#
# Run this after Docker Desktop restarts or whenever the server is not responding.

$env:DB_DIR          = "D:\demo\workdir\long_term_memory\chroma_db_experience"
$env:JSON_DATA_PATH  = "D:\demo\workdir\long_term_memory\experience_data.json"
$env:HOST            = "0.0.0.0"
$env:PORT            = "9030"
$env:MODEL_PATH      = "Qwen/Qwen3-Embedding-0.6B"
# Use local cache; skip HuggingFace network checks (avoids WinError 10061 on
# corporate networks or after Docker Desktop restarts that break connectivity).
$env:TRANSFORMERS_OFFLINE = "1"
$env:HF_HUB_OFFLINE       = "1"

Write-Host "[ltm] Starting experience_server on 0.0.0.0:9030 ..."
& D:\demo\.venv-ltm\Scripts\python.exe D:\demo\tools\experience_server.py
