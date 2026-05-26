import hashlib
import time, os
import random
import string
import requests
from google.colab import drive
from datetime import datetime
drive.mount("/content/drive")

# ===================== 설정 =====================
# Aqara Open API 엔드포인트 / 인증값.
# 비밀값(APPID/KEYID/APPKEY)은 환경변수로만 로드 (CLAUDE.md §2.5 / §6 — 하드코딩 금지).
# Colab 사용 시 노트북 상단에서 os.environ["AQARA_APPID"]=... 형태로 주입 후 import.
API_URL = "https://open-kr.aqara.com/v3.0/open/api"


def _require_env(name: str) -> str:
    val = os.environ.get(name)
    if not val:
        raise RuntimeError(
            f"환경변수 {name} 가 설정되어 있지 않습니다. Colab 셀에서 os.environ 으로 주입하거나 .env 를 설정하세요."
        )
    return val


APPID = _require_env("AQARA_APPID")
KEYID = _require_env("AQARA_KEYID")
APPKEY = _require_env("AQARA_APPKEY")

# ================================================

def generate_nonce(length=16):
    return ''.join(random.choices(string.ascii_lowercase + string.digits, k=length))

def generate_sign(app_id, key_id, app_key, nonce, timestamp):
    """Aqara API 서명 생성"""
    sign_str = f"Appid={app_id}&Keyid={key_id}&Nonce={nonce}&Time={timestamp}{app_key}"
    sign_str = sign_str.lower()
    return hashlib.md5(sign_str.encode()).hexdigest()

def get_headers(app_id, key_id, app_key):
    """공통 헤더 생성"""
    nonce = generate_nonce()
    timestamp = str(int(time.time() * 1000))
    sign = generate_sign(app_id, key_id, app_key, nonce, timestamp)

    return {
        "Content-Type": "application/json",
        "Appid": app_id,
        "Keyid": key_id,
        "Nonce": nonce,
        "Time": timestamp,
        "Sign": sign,
    }

def refresh_token(refresh_token_value: str) -> dict:
    """
    refreshToken으로 새로운 accessToken, refreshToken 발급

    Args:
        refresh_token_value: 현재 보유 중인 refreshToken 문자열

    Returns:
        {
            "accessToken": "...",
            "refreshToken": "...",
            "expiresIn": "86400",
            "openId": "..."
        }
    """
    headers = get_headers(APPID, KEYID, APPKEY)

    payload = {
        "intent": "config.auth.refreshToken",
        "data": {
            "refreshToken": refresh_token_value
        }
    }

    response = requests.post(API_URL, headers=headers, json=payload)
    response.raise_for_status()

    body = response.json()

    if body.get("code") != 0:
        raise Exception(f"Token refresh 실패: code={body.get('code')}, message={body.get('message')}")

    result = body["result"]
    print(f"✅ 토큰 갱신 성공!")
    print(f"   accessToken : {result['accessToken']}")
    print(f"   refreshToken: {result['refreshToken']}")
    print(f"   expiresIn   : {result['expiresIn']}초")
    print(f"   openId      : {result['openId']}")

    return result


if __name__ == "__main__":
    # 현재 노트북 파일 경로 가져오기
    notebook_path = "/content/drive/MyDrive/Colab Notebooks"
    save_path = os.path.join(notebook_path, "new_tokens.txt")
    # 저장된 토큰 파일이 있으면 그 값을 우선 사용. 없으면 환경변수에서 1회용 부트스트랩 토큰 로드.
    # 한 번 꼬이면 Aqara 개발자 콘솔에서 새 refresh_token 발급 → AQARA_REFRESH_TOKEN 환경변수에 주입 후 재실행.
    if os.path.exists(save_path):
        with open(save_path, "r") as f:
            lines = f.readlines()
            current_refresh_token = lines[1].strip().replace("refreshToken: ", "")
    else:
        current_refresh_token = _require_env("AQARA_REFRESH_TOKEN")

    print(f"Current Refresh Token: {current_refresh_token}")
    # refresh 토큰으로 access 토큰 갱신
    new_tokens = refresh_token(current_refresh_token)

    # 갱신된 토큰을 저장/사용
    new_access_token = new_tokens["accessToken"]
    new_refresh_token = new_tokens["refreshToken"]

    # txt 파일으로 저장

    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    with open(save_path, "w") as f:
        f.write(new_access_token + "\n")
        f.write(new_refresh_token + "\n")
        f.write(f"갱신일: {now}")
        print(f"저장 경로: {os.path.abspath(save_path)}")
