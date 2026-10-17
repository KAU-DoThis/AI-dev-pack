#!/bin/bash
# WSL 안에서 jeff-serve 켜기: v1.3 베이스 + 어댑터 폴더 (학습 탭이 끝난 어댑터를 여기에 넣고 서버에 다시 읽으라고 한다).
# 요청의 "model" 이 베이스 이름이면 학습 전, 어댑터 폴더 이름이면 그 어댑터로 판단한다.
# 윈도우에서: wsl.exe -d Ubuntu-24.04 -u user -- bash /mnt/c/.../engines/jeff/wsl/serve.sh
# WSL2 는 127.0.0.1 포트를 윈도우와 같이 쓰므로 웹 UI 주소(127.0.0.1:8765)는 그대로.
set -e
R=${JEFF_TRAIN_ROOT:-$HOME/jeff-train}
mkdir -p "$R/adapters"
cd "$R/jeff"
export JEFF_CHECKPOINT="$R/models/jeff-base-v1.3" JEFF_ADAPTERS="$R/adapters" \
       JEFF_HOST=127.0.0.1 PORT=${PORT:-8765} HF_HOME="$R/.cache/hf" PYTHONIOENCODING=utf-8
exec .venv/bin/jeff-serve
