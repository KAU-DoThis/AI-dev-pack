"""데이터셋 탭 로직 (gradio 없이 테스트 가능).

흐름: ① 파일 불러오기 → 빈 칸 채우기(id·suite·family·question·target·source) → 형식 검사
      ② 이름 붙여 저장 → 분할(family 단위) → 누수 검사 → 지름길 보고서 → 잠금(시험셋 읽기 전용 + 파일 해시)
결과는 profiles/<프로필>/data/<이름>/ (커밋 안 함). 학습 탭은 이 폴더를 고르고, 시작 전에 ready_problem 으로 막는다.
한 줄에 꼭 있어야 하는 것: state(dict) 와 label(프로필 라벨 중 하나). 정답 없는 줄은 받지 않는다 (정답은 사람이 정함).
"""

import hashlib
import json
import os
import re
import shutil
import stat
from datetime import datetime
from pathlib import Path

import pandas as pd

from engines.base import EngineUnavailable, can_prepare
from loop.core.config import action_labels, load_profile, load_prompt, profile_dir
from loop.core.engine import Engine, get_engine
from loop.core.paths import ROOT

SPLIT_FILES = ("train", "development", "calibration", "test")
ROWS_FILE = "rows.jsonl"          # 채운 원본 (분할 전)
LOCK_FILE = "lock.json"           # 파일 해시·누수 검사 결과
STAGING = ".loaded.jsonl"         # ① 에서 채우고 검사 통과한 파일. ② 가 이름 붙여 폴더로 복사
SAMPLE_DATA = "logs/smoke-train/split"  # 견본 (학습 도구 동작 확인용 60줄, 잠금 없음)
NAME_RULE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
WINDOWS_RESERVED = re.compile(r"^(con|prn|aux|nul|com\d|lpt\d)(\..*)?$", re.IGNORECASE)
LEAK_FOUND = "leak check FAILED"  # 도구가 진짜 누수를 찾았을 때 출력 (그 밖의 실패는 도구 오류)
PREVIEW_ROWS = 200
PREVIEW_COLUMNS = ["id", "family", "label", "state"]
LIST_COLUMNS = ["이름", "train", "development", "calibration", "test", "누수 검사", "잠금"]
NOT_SUPPORTED = "이 엔진은 데이터 검사·분할을 지원하지 않아."


def data_root() -> Path:
    return profile_dir() / "data"


def default_name() -> str:
    return f"data-{datetime.now():%m%d-%H%M}"


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# ---- 불러오기: 읽기 · 빈 칸 채우기 ----
def read_lines(path: Path) -> tuple[list[dict], list[str]]:
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        return [], ["파일이 UTF-8 이 아니야. 편집기에서 인코딩을 UTF-8 로 골라 다시 저장해 줘."]
    rows, problems = [], []
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError as error:
            problems.append(f"{number}번째 줄이 JSON 이 아니야 ({error.msg})")
            continue
        if not isinstance(row, dict):
            problems.append(f"{number}번째 줄이 {{...}} 모양이 아니야")
            continue
        row["_line"] = number
        rows.append(row)
    return rows, problems


def family_key(text: str) -> str:
    """family 비교용으로 고르게: 소문자, 공백 하나로, 한글과 숫자 사이 공백 없앰 ("이마트 24" = "이마트24")."""
    text = " ".join(str(text).split()).lower()
    return re.sub(r"(?<=[가-힣]) (?=\d)|(?<=\d) (?=[가-힣])", "", text)


def family_of(state: dict) -> str:
    """묶음 이름: 프로필 display_keys 중 처음 있는 칸(없으면 첫 글자 칸)에서 괄호·지점 표시를 뗀 것.
    같은 묶음은 분할 때 한쪽(학습 또는 시험)으로만 간다. 띄어쓰기 없이 붙은 지점명은 못 떼니 미리보기에서 확인."""
    keys = [k for k in load_profile()["display_keys"] if isinstance(state.get(k), str) and state[k].strip()]
    text = state[keys[0]] if keys else next((v for v in state.values() if isinstance(v, str) and v.strip()), "")
    text = re.sub(r"\(.*?\)|㈜", " ", text)
    words = text.split()
    if len(words) > 1 and re.search(r"(\d+호)?점$", words[-1]):  # "OO 강남역점", "OO 2호점" → "OO"
        words = words[:-1]
    elif len(words) == 1:
        words = [re.sub(r"\s*\d+호점$", "", words[0])]
    return family_key(" ".join(words))


def fill(rows: list[dict], source: str) -> tuple[list[dict], list[str], list[str]]:
    """빈 칸을 채운 줄, 문제(있으면 저장 안 함), 알림(채운 것 요약)."""
    labels = action_labels()
    prompt = load_prompt("en")
    suite = re.sub(r"[^a-z0-9]+", "-", (load_profile()["name"] or "data").lower()).strip("-") or "data"
    question = {"type": "choice", "instructions": prompt["instructions"], "criteria": prompt["options"]}
    problems, filled, counts, seen = [], [], {"id": 0, "family": 0, "question": 0}, set()
    empty = [label for label, text in prompt["options"].items() if not text.strip()]
    for index, row in enumerate(rows, 1):
        where = f"{row.pop('_line', index)}번째 줄"
        state, label, given = row.get("state"), row.get("label"), row.get("question")
        if not isinstance(state, dict) or not state:
            problems.append(f"{where}: state 가 없어 ({{...}} 모양이어야 해)")
            continue
        if label in (None, ""):
            problems.append(f"{where}: 정답(label)이 없어")
            continue
        if given is not None and not (isinstance(given, dict) and isinstance(given.get("criteria"), dict)):
            problems.append(f"{where}: question 은 {{\"criteria\": {{선택지: 설명}}}} 모양이어야 해 (없으면 프로필에서 채움)")
            continue
        own = given["criteria"] if given is not None else None
        if own is None and label not in labels:
            problems.append(f"{where}: 정답 `{label}` 이 프로필 라벨({', '.join(labels)})에 없어")
            continue
        if own is not None and label not in own:
            problems.append(f"{where}: 정답 `{label}` 이 이 줄의 선택지에 없어")
            continue
        family = family_key(row["family"]) if row.get("family") else family_of(state)
        out = {"id": row.get("id") or f"{source}-{index:04d}", "suite": row.get("suite") or suite,
               "family": family or f"{source}-{index:04d}", "state": state,
               "question": given or question, "label": label,
               "target": row.get("target", label), "source": row.get("source") or {"dataset": source}}
        counts["id"] += not row.get("id")
        counts["family"] += not row.get("family")
        counts["question"] += own is None
        if out["id"] in seen:
            problems.append(f"{where}: id `{out['id']}` 가 겹쳐")
            continue
        seen.add(out["id"])
        filled.append(out)
    if counts["question"] and empty:
        problems.append(f"프로필 선택지 설명이 비어 있어: {', '.join(empty)} (prompt.en.yaml)")
    notes = [f"{what} {n}줄" for what, n in (("id", counts["id"]), ("family", counts["family"]),
                                              ("question(프로필 지시문·선택지)", counts["question"])) if n]
    return filled, problems, notes


def preview(rows: list[dict]) -> pd.DataFrame:
    shown = [[r["id"], r["family"], r["label"], json.dumps(r["state"], ensure_ascii=False)[:80]]
             for r in rows[:PREVIEW_ROWS]]
    return pd.DataFrame(shown, columns=PREVIEW_COLUMNS)


def counts_markdown(rows: list[dict]) -> str:
    by_label: dict[str, int] = {}
    for r in rows:
        by_label[r["label"]] = by_label.get(r["label"], 0) + 1
    families = len({r["family"] for r in rows})
    parts = " · ".join(f"`{k}` {v}" for k, v in sorted(by_label.items(), key=lambda kv: -kv[1]))
    return f"{len(rows)}줄 · family {families}개 · 정답별: {parts}"


def write_jsonl(path: Path, rows: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")


def load(file, engine: Engine | None = None) -> tuple[str, pd.DataFrame, str]:
    """불러오기·검사 → (상태 글, 미리보기, 토큰). 통과하면 STAGING 에 두고 토큰(내용 해시)을 준다.
    나누기는 같은 토큰을 받아야 한다 (다른 파일을 고르고 불러오기 없이 나누기를 누르는 것을 막음)."""
    empty = pd.DataFrame(columns=PREVIEW_COLUMNS)
    engine = engine or get_engine()
    if not can_prepare(engine):
        return f"❌ {NOT_SUPPORTED}", empty, ""
    if not file:
        return "❌ 파일을 먼저 골라 줘 (.jsonl).", empty, ""
    path = Path(file)
    rows, problems = read_lines(path)
    filled, more, notes = fill(rows, re.sub(r"[^A-Za-z0-9_-]+", "-", path.stem).strip("-") or "data")
    problems += more
    if problems:
        shown = "\n".join(f"- {p}" for p in problems[:20])
        rest = f"\n- … 외 {len(problems) - 20}개" if len(problems) > 20 else ""
        return f"❌ 고칠 게 {len(problems)}개 있어. 저장 안 함.\n\n{shown}{rest}", preview(filled), ""
    if not filled:
        return "❌ 빈 파일이야.", empty, ""
    staging = data_root() / STAGING
    checking = staging.with_suffix(".checking")  # 검사 통과 전에는 STAGING 을 건드리지 않는다
    write_jsonl(checking, filled)
    try:
        result = engine.check_rows(checking)
    except EngineUnavailable as error:
        checking.unlink(missing_ok=True)
        return f"❌ {error}", preview(filled), ""
    if not result.ok:
        checking.unlink(missing_ok=True)
        return f"❌ 형식 검사 실패\n\n```\n{result.message}\n```", preview(filled), ""
    os.replace(checking, staging)
    filled_note = f"\n\n자동으로 채운 칸: {', '.join(notes)}" if notes else ""
    return (f"✅ 불러오기·검사 통과 — {counts_markdown(filled)}{filled_note}\n\n"
            f"family 는 같은 이름끼리 묶은 거야. 미리보기에서 이상하면 파일에 family 를 직접 적어 줘.\n\n"
            f"👉 다음: **나누기·검사·잠그기**"), preview(filled), sha256(staging)


# ---- 나누기 · 누수 · 지름길 · 잠금 ----
def line_count(path: Path) -> int:
    return sum(1 for line in path.read_text(encoding="utf-8").splitlines() if line.strip()) if path.exists() else 0


def locked_files(folder: Path) -> list[Path]:
    return [folder / ROWS_FILE, *(folder / f"{name}.jsonl" for name in SPLIT_FILES)]


def lock(folder: Path, leak_ok: bool) -> dict:
    """모든 파일 해시 기록 + 시험셋 읽기 전용. 나중에 하나라도 바뀌면 lock_state 가 알려 주고 학습이 막힌다."""
    info = {"sha256": {p.name: sha256(p) for p in locked_files(folder)},
            "locked_at": datetime.now().isoformat(timespec="seconds"), "leak_ok": leak_ok,
            "rows": {name: line_count(folder / f"{name}.jsonl") for name in SPLIT_FILES}}
    (folder / LOCK_FILE).write_text(json.dumps(info, ensure_ascii=False, indent=2), encoding="utf-8")
    os.chmod(folder / "test.jsonl", stat.S_IREAD)
    return info


def read_lock(folder: Path) -> dict | None:
    try:
        return json.loads((folder / LOCK_FILE).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def lock_state(folder: Path) -> str:
    info = read_lock(folder)
    if info is None:
        return "잠금 없음"
    changed = [name for name, digest in (info.get("sha256") or {}).items()
               if not (folder / name).exists() or sha256(folder / name) != digest]
    return f"⚠️ 바뀜: {', '.join(changed)}" if changed else "🔒 그대로"


def ready_problem(folder: Path) -> str:
    """학습 시작 전 검사. 막아야 하면 이유, 괜찮으면 빈 문자열. 잠금 없는 폴더(견본·직접 만든 것)는 막지 않는다."""
    info = read_lock(folder)
    if info is None:
        return ""
    if info.get("leak_ok") is not True:
        return "이 데이터셋은 누수 검사를 통과 못 했어 (학습 파일에 평가용 줄이 있음). 데이터를 고쳐서 다시 만들어 줘."
    state = lock_state(folder)
    return "" if state == "🔒 그대로" else f"잠근 뒤 파일이 바뀌었어 ({state}). 데이터셋 탭에서 다시 만들어 줘."


def remove_tree(path: Path) -> None:
    """읽기 전용 파일(잠근 시험셋)이 있어도 지운다 (Windows 는 읽기 전용이면 못 지움)."""
    if not path.exists():
        return
    for item in path.rglob("*"):
        os.chmod(item, stat.S_IWRITE | stat.S_IREAD)
    shutil.rmtree(path, ignore_errors=True)


def count_flags(report: str) -> int:
    """지름길 보고서 '## Flags' 아래 '- ' 줄 수. 깃발이 없으면 도구가 '- none: …' 한 줄을 쓴다."""
    section = report.split("## Flags", 1)[-1].split("\n## ", 1)[0] if "## Flags" in report else ""
    return sum(1 for line in section.splitlines() if line.startswith("- ") and not line.startswith("- none:"))


def split(name, test, development, calibration, seed, engine: Engine | None = None,
          token: str | None = None) -> tuple[str, str]:
    """(상태 글, 지름길 보고서 markdown). token 은 ① 이 준 값 (화면은 항상 넘김)."""
    engine = engine or get_engine()
    if not can_prepare(engine):
        return f"❌ {NOT_SUPPORTED}", ""
    name = str(name or "").strip()
    if not NAME_RULE.match(name) or WINDOWS_RESERVED.match(name) or name.endswith("."):
        return "❌ 이름은 영문·숫자·`-`·`_`·`.` 만 (64자까지, 첫 글자는 영문·숫자, 끝은 `.` 아님, CON·NUL 같은 이름 안 됨).", ""
    try:
        test, development, calibration, seed = float(test), float(development), float(calibration), int(seed)
    except (TypeError, ValueError):
        return "❌ 비율·seed 에 숫자를 넣어 줘.", ""
    if not (0 < test < 1 and 0 < development < 1 and 0 < calibration < 1
            and round(test + development + calibration, 6) < 0.9):
        return "❌ 비율은 0 과 1 사이, 셋을 더해 0.9 미만 (학습에 남는 몫이 있게).", ""
    staging = data_root() / STAGING
    if not staging.exists() or (token is not None and token != sha256(staging)):
        return "❌ 먼저 **불러오기·검사**를 통과해야 해 (파일을 바꿨으면 불러오기부터 다시).", ""
    folder = data_root() / name
    if folder.exists():
        return f"❌ `{name}` 은(는) 이미 있어. 다른 이름으로 해 줘.", ""
    work = data_root() / f".making-{name}"  # 다 만들고 잠근 뒤에만 진짜 이름으로 → 반쯤 만든 폴더가 목록에 안 보임
    remove_tree(work)
    try:
        work.mkdir(parents=True)
        shutil.copyfile(staging, work / ROWS_FILE)
        done = engine.split(work / ROWS_FILE, work, test, development, calibration, seed)
        if not done.ok:
            remove_tree(work)
            return f"❌ 분할 실패\n\n```\n{done.message}\n```", ""
        leak = engine.leak_check(work)
        if not leak.ok and LEAK_FOUND not in leak.message:
            remove_tree(work)
            return f"❌ 누수 검사 도구 오류 (누수가 아니라 실행 실패)\n\n```\n{leak.message}\n```", ""
        shortcut = engine.shortcut_report(work)
        info = lock(work, leak.ok)
        os.rename(work, folder)
    except (EngineUnavailable, OSError) as error:
        remove_tree(work)
        return f"❌ {error}", ""
    if token is None or token == sha256(staging):
        staging.unlink(missing_ok=True)
    if not shortcut.ok:
        shortcut_line = f"- ⚠️ 지름길 보고서를 못 만들었어 (분할·잠금은 됨)\n\n```\n{shortcut.message}\n```"
    elif flags := count_flags(shortcut.report):
        shortcut_line = f"- ⚠️ 지름길 보고서 깃발 {flags}개 — 아래 보고서를 한 번 봐 줘 (깃발이 꼭 문제는 아님)."
    else:
        shortcut_line = "- ✅ 지름길 보고서 깃발 없음."
    lines = [
        f"✅ `{name}` 만들었어 → `{shown(folder)}`",
        "",
        "| 파일 | 줄 수 | 쓰임 |",
        "|---|---|---|",
        f"| train | {info['rows']['train']} | 학습 |",
        f"| development | {info['rows']['development']} | 학습 중 체크포인트 고르기 |",
        f"| calibration | {info['rows']['calibration']} | 확률 보정 |",
        f"| test | {info['rows']['test']} | 🔒 진짜 점수 (학습에 안 씀, 평가 탭에서만) |",
        "",
        ("- ✅ 누수 검사 통과: 학습 파일에 평가용 줄이 없어." if leak.ok
         else f"- ❌ 누수 검사 실패: 학습 파일에 평가용 줄(또는 거의 같은 줄)이 있어. **학습 탭에서 이 데이터는 막혀.**"
              f"\n\n```\n{leak.message}\n```"),
        shortcut_line,
        f"- 🔒 잠금: 시험셋 읽기 전용 + 파일 5개 해시 (바뀌면 학습이 막힘)",
        "",
        "👉 다음: 아래 **② 학습하러 가기 →** (이 데이터셋이 자동으로 골라져)." if leak.ok else "",
    ]
    return "\n".join(lines), shortcut.report


# ---- 목록 (이 탭 아래 표, 학습 탭 드롭다운) ----
def folders() -> list[Path]:
    root = data_root()
    if not root.exists():
        return []
    found = [p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
             and all((p / f"{n}.jsonl").exists() for n in SPLIT_FILES)]
    return sorted(found, key=lambda p: p.stat().st_mtime, reverse=True)


def table() -> pd.DataFrame:
    rows = []
    for folder in folders():
        leak_ok = (read_lock(folder) or {}).get("leak_ok")
        rows.append([folder.name, *(line_count(folder / f"{n}.jsonl") for n in SPLIT_FILES),
                     {True: "✅", False: "❌"}.get(leak_ok, "-"), lock_state(folder)])
    return pd.DataFrame(rows, columns=LIST_COLUMNS)


def shown(path: Path) -> str:
    """웹 UI 폴더 안이면 그 기준 상대 경로 (학습 탭이 그대로 받음), 밖이면 절대 경로."""
    return path.relative_to(ROOT).as_posix() if path.is_relative_to(ROOT) else path.as_posix()


def data_choices() -> list[str]:
    """학습 탭 '데이터 폴더' 후보: 학습해도 되는 것만, 최근 것 먼저, 견본은 맨 뒤."""
    made = [shown(p) for p in folders() if not ready_problem(p)]
    return made + ([SAMPLE_DATA] if (ROOT / SAMPLE_DATA).exists() else [])
