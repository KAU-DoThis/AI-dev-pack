from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent  # 웹 UI 폴더 맨 위 (loop/core/paths.py 기준 세 단계 위)

CONFIG_DIR = ROOT / "config"      # 웹 UI 공용 설정 (서버 주소, 쓸 프로필)
PROFILES_DIR = ROOT / "profiles"  # 프로젝트 하나 = profiles/<이름>/ (라벨·설명·예시 + data/·outputs/)
ENGINES_DIR = ROOT / "engines"    # 모델 하나 = engines/<이름>/ (클라이언트·가중치)
LOGS_DIR = ROOT / "logs"          # 서버 로그·백업·비교 기록 (커밋 안 함)
