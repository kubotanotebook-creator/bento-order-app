"""管理者へのメール通知。

PythonAnywhere の無料プランは外部接続が許可リスト制で、SMTP(25/587番)は
使えない。許可されているメール配信サービスのHTTP APIを使う必要がある。
ここでは Brevo (api.brevo.com) を使う。

環境変数が設定されていなければ何もしない(送信を試みずに False を返す)ので、
未設定のまま動かしてもアプリはそのまま動作する。

  BENTO_MAIL_API_KEY   Brevo の APIキー
  BENTO_MAIL_TO        宛先。複数はカンマ区切り
  BENTO_MAIL_FROM      差出人。Brevo で認証済みのアドレスであること
  BENTO_MAIL_FROM_NAME 差出人の表示名(省略時「まつうランチ」)
"""
import json
import os
import sys
import urllib.error
import urllib.request

BREVO_ENDPOINT = "https://api.brevo.com/v3/smtp/email"
TIMEOUT_SECONDS = 10


def mail_config():
    """設定が揃っていれば dict、足りなければ None。"""
    api_key = os.environ.get("BENTO_MAIL_API_KEY", "").strip()
    to = os.environ.get("BENTO_MAIL_TO", "").strip()
    sender = os.environ.get("BENTO_MAIL_FROM", "").strip()
    if not (api_key and to and sender):
        return None
    return {
        "api_key": api_key,
        "to": [a.strip() for a in to.split(",") if a.strip()],
        "from": sender,
        "from_name": os.environ.get("BENTO_MAIL_FROM_NAME", "まつうランチ").strip(),
    }


def is_configured():
    return mail_config() is not None


def send_mail(subject, body):
    """メールを送る。成功なら (True, None)、失敗なら (False, 理由)。

    通知の失敗で業務側の操作を止めないよう、例外は投げずに戻り値で返す。
    """
    cfg = mail_config()
    if not cfg:
        return False, "メール送信の環境変数が設定されていません。"

    payload = {
        "sender": {"email": cfg["from"], "name": cfg["from_name"]},
        "to": [{"email": addr} for addr in cfg["to"]],
        "subject": subject,
        "textContent": body,
    }
    req = urllib.request.Request(
        BREVO_ENDPOINT,
        data=json.dumps(payload).encode("utf-8"),
        headers={
            "api-key": cfg["api_key"],
            "content-type": "application/json",
            "accept": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_SECONDS) as res:
            res.read()
        return True, None
    except urllib.error.HTTPError as e:
        # 本文にBrevo側の理由(送信元未認証・キー誤りなど)が入るので残す
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:
            detail = ""
        reason = f"メール送信に失敗しました(HTTP {e.code}) {detail}"
    except Exception as e:
        reason = f"メール送信に失敗しました: {e}"

    print("WARNING: " + reason, file=sys.stderr)
    return False, reason
