#!/bin/bash
# 학습 탭이 부르는 스크립트 (WSL 안에서 실행). Jeff 원본 jeff-train 을 그대로 부른다.
#   bash train.sh <이름> <데이터 폴더(리눅스 경로)> <epochs> <batch> <effective batch> <lr> <lora rank> <base model> <revision>
# 결과: $R/runs/<이름>/ (data/ 복사본, run/ 기록, out/ 체크포인트, train.log, state)
# 성공하면 고른 체크포인트(out/selected)를 $R/adapters/<이름>/ 로 복사 → 서버가 다시 읽으면 추론 탭에서 고를 수 있다.
# 시험셋(test.jsonl)은 복사하지도 넘기지도 않는다 (학습·체크포인트 고르기에 시험셋이 섞이면 점수가 거짓이 됨).
# state 파일: running → done | stopped | failed <코드>
set -u
name=$1 data=$2 epochs=$3 batch=$4 eff=$5 lr=$6 rank=$7 base_model=$8 revision=$9
R=${JEFF_TRAIN_ROOT:-$HOME/jeff-train}
D="$R/runs/$name"
if [ -e "$D" ] || [ -e "$R/adapters/$name" ]; then echo "이미 있는 이름: $name" >&2; exit 2; fi
mkdir -p "$D/data"
# 화면이 처음부터 전체 step 수를 알게 (jeff-train 의 run/config.json 은 모델을 다 불러온 뒤에야 생김)
echo "{\"epochs\": $epochs, \"effective_batch_size\": $eff}" > "$D/params.json"
echo running > "$D/state"
for f in train development calibration; do
  if ! cp "$data/$f.jsonl" "$D/data/$f.jsonl"; then echo "failed 2" > "$D/state"; exit 2; fi
done

cd "$R/jeff"
export JEFF_EVENTS="$D/events.jsonl" HF_HOME="$R/.cache/hf" PYTHONIOENCODING=utf-8
.venv/bin/jeff-train \
  --train "$D/data/train.jsonl" --development "$D/data/development.jsonl" --temperature "$D/data/calibration.jsonl" \
  --run "$D/run" --output "$D/out" \
  --initial-checkpoint "$R/models/jeff-base-v1.3" --base-model "$base_model" --revision "$revision" \
  --lora-rank "$rank" --epochs "$epochs" --batch-size "$batch" --effective-batch-size "$eff" --lr "$lr" \
  --eval-every 2 --cpu-threads 8 > "$D/train.log" 2>&1 &
echo $! > "$D/pid"
wait $!
code=$?

if [ -e "$D/stop" ]; then
  echo stopped > "$D/state"
elif [ $code -eq 0 ] && [ -d "$D/out/selected" ]; then
  mkdir -p "$R/adapters"
  if cp -rL "$D/out/selected" "$R/adapters/$name"; then echo done > "$D/state"; else echo "failed copy" > "$D/state"; fi
else
  echo "failed $code" > "$D/state"
fi
exit $code
