import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# 테스트는 어느 컴퓨터·레포에서나 같은 결과가 나오게 항상 예시 프로필로 (settings.local.yaml 무시).
# loop.core.config 가 import 될 때 프로필 경로를 정하므로 import 전에 정해야 한다.
os.environ["WEBUI_PROFILE"] = "example"
