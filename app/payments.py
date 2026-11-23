import asyncio
import base64
import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
import uuid
from sqlalchemy import select
from app.database import AppSetting


async def load_payment_cfg(session) -> dict:
    res = await session.execute(select(AppSetting))
    raw = {row.key: row.value for row in res.scalars().all()}
    return {
        "pay_provider": (raw.get("pay_provider") or "manual").strip().lower(),
        "yk_shop_id": (raw.get("yk_shop_id") or "").strip(),
        "yk_secret_key": (raw.get("yk_secret_key") or "").strip(),
        "rk_login": (raw.get("rk_login") or "").strip(),
        "rk_pass1": (raw.get("rk_pass1") or "").strip(),
        "rk_pass2": (raw.get("rk_pass2") or "").strip(),
        "tb_terminal_key": (raw.get("tb_terminal_key") or "").strip(),
        "tb_password": (raw.get("tb_password") or "").strip(),
        "cp_public_id": (raw.get("cp_public_id") or "").strip(),
    }


def _http_post_json(url: str, payload: dict, headers: dict | None = None) -> tuple[int, dict]:
    data = json.dumps(payload).encode("utf-8")
    req_headers = {"Content-Type": "application/json"}
    if headers:
        req_headers.update(headers)
    req = urllib.request.Request(url, data=data, headers=req_headers, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="ignore")
        try:
            return e.code, json.loads(body)
        except Exception:
            return e.code, {"description": body or str(e)}
    except Exception as e:
        return 0, {"description": str(e)}


async def create_payment_for_order(session, order, return_url: str) -> dict:
    cfg = await load_payment_cfg(session)
    provider = cfg["pay_provider"]
    amount_str = f"{float(order.amount):.2f}"
    desc = f"Оплата парковки, заявка #{order.id}"

    if provider in ("", "manual", "sbp", "none"):
        return {"provider": "manual", "payment_url": None, "error": None}

    if provider == "yookassa":
        shop_id, secret = cfg["yk_shop_id"], cfg["yk_secret_key"]
        if not shop_id or not secret:
            return {"provider": "yookassa", "payment_url": None, "error": "В админке не заполнены Shop ID и Secret Key для ЮKassa."}
        auth = base64.b64encode(f"{shop_id}:{secret}".encode()).decode()
        payload = {
            "amount": {"value": amount_str, "currency": "RUB"},
            "capture": True,
            "confirmation": {"type": "redirect", "return_url": return_url},
            "description": desc,
            "metadata": {"order_id": str(order.id)},
        }
        status, resp = await asyncio.to_thread(
            _http_post_json,
            "https://api.yookassa.ru/v3/payments",
            payload,
            {"Authorization": f"Basic {auth}", "Idempotence-Key": str(uuid.uuid4())},
        )
        url = (resp.get("confirmation") or {}).get("confirmation_url")
        if status in (200, 201) and url:
            return {"provider": "yookassa", "payment_url": url, "error": None}
        err = resp.get("description") or resp.get("code") or f"Ошибка ЮKassa (HTTP {status})"
        return {"provider": "yookassa", "payment_url": None, "error": err}

    if provider == "robokassa":
        login, pass1 = cfg["rk_login"], cfg["rk_pass1"]
        if not login or not pass1:
            return {"provider": "robokassa", "payment_url": None, "error": "В админке не заполнены MerchantLogin и Пароль #1 для Robokassa."}
        sig_src = f"{login}:{amount_str}:{order.id}:{pass1}"
        sig = hashlib.md5(sig_src.encode("utf-8")).hexdigest()
        qs = urllib.parse.urlencode({
            "MerchantLogin": login,
            "OutSum": amount_str,
            "InvId": str(order.id),
            "Description": desc,
            "SignatureValue": sig,
        })
        return {
            "provider": "robokassa",
            "payment_url": f"https://auth.robokassa.ru/Merchant/Index.aspx?{qs}",
            "error": None,
        }

    if provider == "tbank":
        tkey, tpass = cfg["tb_terminal_key"], cfg["tb_password"]
        if not tkey or not tpass:
            return {"provider": "tbank", "payment_url": None, "error": "В админке не заполнены TerminalKey и Пароль для Т-Банка."}
        amount_kop = int(round(float(order.amount) * 100))
        token_data = {
            "Amount": str(amount_kop),
            "Description": desc,
            "OrderId": str(order.id),
            "Password": tpass,
            "TerminalKey": tkey,
        }
        token_str = "".join(token_data[k] for k in sorted(token_data))
        token = hashlib.sha256(token_str.encode("utf-8")).hexdigest()
        payload = {
            "TerminalKey": tkey,
            "Amount": amount_kop,
            "OrderId": str(order.id),
            "Description": desc,
            "Token": token,
            "SuccessURL": return_url,
        }
        status, resp = await asyncio.to_thread(
            _http_post_json,
            "https://securepay.tinkoff.ru/v2/Init",
            payload,
        )
        if resp.get("Success") and resp.get("PaymentURL"):
            return {"provider": "tbank", "payment_url": resp["PaymentURL"], "error": None}
        return {"provider": "tbank", "payment_url": None, "error": resp.get("Message") or resp.get("Details") or "Ошибка Т-Банка"}

    return {"provider": provider, "payment_url": None, "error": f"Провайдер {provider} пока не настроен для прямой ссылки."}
