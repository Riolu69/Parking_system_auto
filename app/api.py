from app.payments import create_payment_for_order
import secrets
import segno
import asyncio
import csv
import io
import logging
import time
from datetime import datetime
from contextlib import asynccontextmanager
from fastapi import FastAPI, Depends, HTTPException, Header
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload

from app.config import settings
from app.database import (
    init_db, async_session, User, Vehicle, SmsQueue, Tariff, Order, PromoCode, RtuSlot, ExtraService, ServiceOrder
)
from app.services import (
    normalize_phone, normalize_plate, register_user_and_car, create_order, confirm_order, reject_order,
    grant_access_days, revoke_access, get_next_sms_task, report_sms_task,
    validate_promo_code, parse_tariff_ids, get_app_settings, set_app_settings,
    get_payment_settings, set_payment_settings, mark_order_paid_by_user,
    process_expired_slots, pop_expiring_slots, queue_open_gate, get_finance_stats,
    set_user_spot, set_vehicle_spot, pop_expired_vehicles_for_check, mark_vehicle_overstay, get_daily_tariff_price, calc_vehicle_debt, create_service_order, get_spots_availability
)
from app.bot import (
    bot, dp, notify_admins_order, notify_user_activated,
    notify_user_expiring, notify_admins_text, send_db_backup_to_admins, broadcast_to_users, notify_admins_support, notify_admins_expired_vehicle
)

logger = logging.getLogger("uvicorn")

worker_state = {
    "last_ping": None,
    "alert_sent": False,
    "last_incoming_sms": None,
    "last_backup_day": None,
}


async def run_bot():
    try:
        await bot.delete_webhook(drop_pending_updates=True)
        logger.info("Telegram-бот запущен.")
        await dp.start_polling(bot, handle_signals=False)
    except asyncio.CancelledError:
        pass
    except Exception as e:
        logger.error(f"Ошибка запуска Telegram-бота: {e}")


async def scheduler_loop():
    while True:
        try:
            async with async_session() as session:
                expired_count = await process_expired_slots(session)
                expired_vehs = await pop_expired_vehicles_for_check(session)
                for ev in expired_vehs:
                    await notify_admins_expired_vehicle(ev)
                if expired_count:
                    logger.info(f"Автоудаление: {expired_count} просроченных ячеек.")

                expiring = await pop_expiring_slots(session)
                for tg_id, valid_until in expiring:
                    await notify_user_expiring(tg_id, valid_until)

            lp = worker_state["last_ping"]
            if lp is not None and (time.time() - lp) > 120 and not worker_state["alert_sent"]:
                worker_state["alert_sent"] = True
                await notify_admins_text("Внимание! Модем RX100-R4 не выходит на связь более 2 минут.")

            now = datetime.now()
            today_str = now.strftime("%Y-%m-%d")
            if now.hour == 3 and worker_state["last_backup_day"] != today_str:
                worker_state["last_backup_day"] = today_str
                await send_db_backup_to_admins()
        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"Ошибка планировщика: {e}")
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await init_db()
    async with async_session() as _s:
        _cfg = await get_app_settings(_s)
        _saved = (_cfg.get("bot_admin_ids") or "").strip()
        if _saved:
            settings.ADMIN_IDS = _saved
    bot_task = asyncio.create_task(run_bot())
    sched_task = asyncio.create_task(scheduler_loop())
    yield
    sched_task.cancel()
    bot_task.cancel()
    for t in (sched_task, bot_task):
        try:
            await t
        except asyncio.CancelledError:
            pass
    await bot.session.close()


app = FastAPI(title="Parking RTU5024 API", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


def verify_worker(authorization: str = Header(None)):
    if not authorization or not secrets.compare_digest(authorization, f"Bearer {settings.WORKER_API_TOKEN}"):
        raise HTTPException(status_code=401, detail="Invalid worker token")


def verify_admin(x_admin_password: str = Header(None)):
    if not x_admin_password or not secrets.compare_digest(x_admin_password, settings.ADMIN_WEB_PASSWORD):
        raise HTTPException(status_code=403, detail="Invalid admin password")


def verify_payment_admin(x_payment_password: str = Header(None)):
    if not x_payment_password or not secrets.compare_digest(x_payment_password, settings.PAYMENT_ADMIN_PASSWORD):
        raise HTTPException(status_code=403, detail="Неверный пароль раздела платежей")


def parse_date_end(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str.strip(), "%Y-%m-%d").replace(hour=23, minute=59, second=59)
    except ValueError:
        raise HTTPException(status_code=400, detail="Неверный формат даты (ожидается ГГГГ-ММ-ДД)")


class RegisterReq(BaseModel):
    phone: str
    plate_number: str
    tariff_id: int
    car_model: str | None = None
    full_name: str | None = None
    promo_code: str | None = None


class PaymentSettingsReq(BaseModel):
    pay_provider: str = "manual"
    pay_auto_renew: str = "0"
    yk_shop_id: str = ""
    yk_secret_key: str = ""
    rk_login: str = ""
    rk_pass1: str = ""
    rk_pass2: str = ""
    tb_terminal_key: str = ""
    tb_password: str = ""
    cp_public_id: str = ""
    cp_api_secret: str = ""


class SettingsReq(BaseModel):
    sbp_phone: str
    sbp_bank: str
    sbp_recipient: str
    gate_phone: str
    bot_username: str = ""
    max_spots: int = 200
    description_enabled: bool = False
    description_text: str = ""
    contract_text: str = ""
    bot_admin_ids: str | None = None


class TariffCreateReq(BaseModel):
    name: str
    price: int
    days: int = 1
    is_night: bool = False
    is_active: bool = True
    description: str = ""


class TariffUpdateReq(TariffCreateReq):
    pass


class SupportReq(BaseModel):
    text: str
    phone: str | None = None
    telegram_id: int | None = None
    full_name: str | None = None


class PromoReq(BaseModel):
    code: str
    discount_percent: int
    valid_until: str | None = None
    target_audience: str = "all"
    tariff_ids: list[int] = []


class GrantReq(BaseModel):
    user_id: int
    days: int = 30


class RevokeReq(BaseModel):
    user_id: int


class SpotUpdateReq(BaseModel):
    spot: str = ""


class ExtraServiceReq(BaseModel):
    name: str
    price: int
    description: str = ""
    is_active: bool = True


class ServiceOrderReq(BaseModel):
    service_id: int
    phone: str
    spot: str = ""
    comment: str = ""


class BroadcastReq(BaseModel):
    text: str


class SmsReportReq(BaseModel):
    status: str
    error_msg: str | None = None


class IncomingSmsReq(BaseModel):
    sender: str
    text: str


@app.get("/")
async def index():
    return FileResponse("static/index.html")


@app.get("/offer")
async def offer_page():
    return FileResponse("static/index.html")


@app.get("/admin")
async def admin_page():
    return FileResponse("static/index.html")


@app.get("/manifest.json")
async def manifest():
    return FileResponse("static/manifest.json")


@app.get("/api/qr")
async def api_qr(data: str):
    buf = io.BytesIO()
    segno.make(data).save(buf, kind="svg", scale=6, border=2)
    return Response(content=buf.getvalue(), media_type="image/svg+xml")


@app.get("/api/settings")
async def api_public_settings():
    async with async_session() as session:
        cfg = await get_app_settings(session)
        avail = await get_spots_availability(session)
        return {**cfg, "free_spots": avail["free_spots"], "used_spots": avail["used_spots"]}


@app.get("/api/gate-contact.vcf")
async def api_gate_vcf():
    async with async_session() as session:
        cfg = await get_app_settings(session)
    phone = cfg["gate_phone"] or settings.RTU_PHONE
    vcf = f"BEGIN:VCARD\nVERSION:3.0\nFN:Шлагбаум Парковка\nTEL;TYPE=CELL:{phone}\nEND:VCARD\n"
    return Response(
        content=vcf,
        media_type="text/vcard; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="gate_parking.vcf"'},
    )


@app.get("/api/tariffs")
async def api_tariffs():
    async with async_session() as session:
        res = await session.execute(select(Tariff).where(Tariff.is_active == True).order_by(Tariff.id))
        return [
            {"id": t.id, "code": t.code, "name": t.name, "price": t.price, "days": t.days, "is_night": t.is_night, "description": t.description or ""}
            for t in res.scalars().all()
        ]


@app.get("/api/promo/{code}")
async def api_check_promo(code: str, phone: str | None = None, tariff_id: int | None = None):
    async with async_session() as session:
        try:
            promo = await validate_promo_code(session, code, phone=phone, tariff_id=tariff_id)
            return {
                "code": promo.code,
                "discount_percent": promo.discount_percent,
                "valid_until": promo.valid_until.strftime("%d.%m.%Y") if promo.valid_until else None,
                "target_audience": promo.target_audience or "all",
                "tariff_ids": parse_tariff_ids(promo.tariff_ids),
            }
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))


@app.post("/api/register")
async def api_register(data: RegisterReq):
    async with async_session() as session:
        user = await register_user_and_car(
            session,
            phone=data.phone,
            plate_number=data.plate_number,
            car_model=data.car_model,
            full_name=data.full_name,
        )
        norm_pl = normalize_plate(data.plate_number)
        v_res = await session.execute(
            select(Vehicle)
            .where(Vehicle.user_id == user.id, Vehicle.plate_number == norm_pl)
            .order_by(Vehicle.id.desc())
            .limit(1)
        )
        matched_veh = v_res.scalar_one_or_none() or (user.vehicles[-1] if user.vehicles else None)
        try:
            order = await create_order(
                session,
                user.id,
                data.tariff_id,
                data.promo_code,
                vehicle_id=matched_veh.id if matched_veh else None,
            )
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        cfg = await get_app_settings(session)
        pay_res = await create_payment_for_order(session, order, return_url="https://parking.vonline.net.ru/")
        return {
            "ok": True,
            "user_id": user.id,
            "order_id": order.id,
            "amount": order.amount,
            "promo_code": order.promo_code,
            "discount_percent": order.discount_percent,
            "phone": user.phone,
            "payment": cfg,
            "pay_provider": pay_res["provider"],
            "payment_url": pay_res["payment_url"],
            "payment_error": pay_res["error"],
        }


@app.post("/api/orders/{order_id}/paid")
async def api_order_paid_by_user(order_id: int):
    async with async_session() as session:
        try:
            order = await mark_order_paid_by_user(session, order_id)
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
    await notify_admins_order(order)
    return {"ok": True}


@app.get("/api/status/{phone}")
async def api_status(phone: str):
    norm = normalize_phone(phone)
    async with async_session() as session:
        res = await session.execute(
            select(User)
            .options(selectinload(User.vehicles), selectinload(User.slot))
            .where(User.phone == norm)
        )
        user = res.scalar_one_or_none()
        cfg = await get_app_settings(session)
        daily_price = await get_daily_tariff_price(session)
        if not user:
            raise HTTPException(status_code=404, detail="Номер не найден")
        return {
            "phone": user.phone,
            "full_name": user.full_name,
            "gate_phone": cfg["gate_phone"],
            "vehicles": [
                {
                    "id": v.id,
                    "plate": v.plate_number,
                    "model": v.car_model,
                    "spot": v.spot_label,
                    "overstay_status": getattr(v, "overstay_status", "none"),
                    "debt_days": calc_vehicle_debt(v, daily_price)[0],
                    "debt_amount": calc_vehicle_debt(v, daily_price)[1],
                    "valid_until": v.valid_until.strftime("%d.%m.%Y %H:%M") if v.valid_until else None,
                }
                for v in user.vehicles
            ],
            "slot": {
                "number": user.slot.slot_number,
                "spot": user.slot.spot_label,
                "status": user.slot.status,
                "valid_until": user.slot.valid_until.strftime("%d.%m.%Y %H:%M") if user.slot.valid_until else None,
            } if user.slot else None,
        }


@app.post("/api/support")
async def api_support(data: SupportReq):
    txt = data.text.strip()
    if not txt:
        raise HTTPException(status_code=400, detail="Введите текст сообщения")
    tg_id = data.telegram_id
    phone = normalize_phone(data.phone) if data.phone else ""
    full_name = data.full_name or ""
    async with async_session() as session:
        user = None
        if tg_id:
            res = await session.execute(select(User).where(User.telegram_id == tg_id))
            user = res.scalar_one_or_none()
        elif phone:
            res = await session.execute(select(User).where(User.phone == phone))
            user = res.scalar_one_or_none()
        if user:
            tg_id = tg_id or user.telegram_id
            phone = phone or user.phone
            full_name = full_name or (user.full_name or "")
    await notify_admins_support(text=txt, user_tg_id=tg_id, phone=phone, full_name=full_name)
    return {"ok": True}


@app.get("/api/extra-services")
async def api_extra_services():
    async with async_session() as session:
        res = await session.execute(select(ExtraService).where(ExtraService.is_active == True).order_by(ExtraService.id))
        return [{"id": x.id, "name": x.name, "price": x.price, "description": x.description or ""} for x in res.scalars().all()]


@app.post("/api/extra-services/order")
async def api_create_service_order(data: ServiceOrderReq):
    async with async_session() as session:
        try:
            order = await create_service_order(session, data.service_id, data.phone, data.spot, data.comment)
            cfg = await get_app_settings(session)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True, "order_id": order.id, "amount": order.amount, "payment": cfg}


@app.post("/api/extra-services/orders/{order_id}/paid")
async def api_service_order_paid(order_id: int):
    async with async_session() as session:
        res = await session.execute(
            select(ServiceOrder)
            .options(selectinload(ServiceOrder.service))
            .where(ServiceOrder.id == order_id)
        )
        order = res.scalar_one_or_none()
        if not order:
            raise HTTPException(status_code=404, detail="Заказ не найден")
        if order.status == "pending":
            order.status = "paid_by_user"
            await session.commit()
        svc_name = order.service.name if order.service else f"Услуга #{order.service_id}"
        phone = order.phone
        spot = order.spot or "—"
        comment = order.comment or "—"
        amount = order.amount

    await notify_admins_text(
        f"Оплата доп. услуги · Заказ #{order_id}\n"
        f"Услуга: {svc_name} ({amount} ₽)\n"
        f"Телефон: +{phone}\n"
        f"Место: {spot}\n"
        f"Комментарий: {comment}"
    )
    return {"ok": True}


# --- Админка ---
@app.get("/api/admin/stats", dependencies=[Depends(verify_admin)])
async def admin_stats():
    async with async_session() as session:
        avail = await get_spots_availability(session)
        fin = await get_finance_stats(session)
    lp = worker_state["last_ping"]
    online = lp is not None and (time.time() - lp) <= 120
    return {
        "slots_used": avail["used_spots"],
        "slots_total": avail["max_spots"],
        "free_spots": avail["free_spots"],
        "worker_online": online,
        "last_incoming_sms": worker_state["last_incoming_sms"],
        "finance": fin,
    }


@app.post("/api/admin/open-gate", dependencies=[Depends(verify_admin)])
async def admin_open_gate():
    async with async_session() as session:
        await queue_open_gate(session)
    return {"ok": True}


@app.post("/api/admin/broadcast", dependencies=[Depends(verify_admin)])
async def admin_broadcast(data: BroadcastReq):
    if not data.text.strip():
        raise HTTPException(status_code=400, detail="Пустой текст")
    sent = await broadcast_to_users(data.text.strip())
    return {"ok": True, "sent": sent}


@app.post("/api/admin/backup-now", dependencies=[Depends(verify_admin)])
async def admin_backup_now():
    await send_db_backup_to_admins()
    return {"ok": True}


@app.get("/api/admin/export/users", dependencies=[Depends(verify_admin)])
async def admin_export_users():
    async with async_session() as session:
        res = await session.execute(select(User).options(selectinload(User.vehicles), selectinload(User.slot)).order_by(User.id))
        users = res.scalars().all()
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(["ID", "Телефон", "Имя", "Машины", "Ячейка", "Место", "Статус", "Действует до"])
    for u in users:
        cars = ", ".join([v.plate_number for v in u.vehicles])
        w.writerow([
            u.id, f"+{u.phone}", u.full_name or "", cars,
            u.slot.slot_number if u.slot else "",
            u.slot.spot_label if (u.slot and u.slot.spot_label) else "",
            u.slot.status if u.slot else "",
            u.slot.valid_until.strftime("%d.%m.%Y %H:%M") if (u.slot and u.slot.valid_until) else "",
        ])
    return Response(content=("\ufeff" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8")


@app.get("/api/admin/payments/settings", dependencies=[Depends(verify_admin), Depends(verify_payment_admin)])
async def admin_get_payment_settings():
    async with async_session() as session:
        return await get_payment_settings(session)


@app.put("/api/admin/payments/settings", dependencies=[Depends(verify_admin), Depends(verify_payment_admin)])
async def admin_set_payment_settings(data: PaymentSettingsReq):
    async with async_session() as session:
        await set_payment_settings(session, data.model_dump())
        return {"ok": True}


@app.put("/api/admin/settings", dependencies=[Depends(verify_admin)])
async def admin_set_settings(data: SettingsReq):
    async with async_session() as session:
        payload = data.model_dump()
        payload["max_spots"] = max(0, int(payload.get("max_spots", 200)))
        payload["description_enabled"] = "1" if payload.get("description_enabled") else "0"
        if payload.get("bot_admin_ids") is not None:
            _ids = sorted(set(int(x.strip()) for x in str(payload["bot_admin_ids"]).replace(";", ",").replace(" ", ",").split(",") if x.strip().isdigit() and int(x.strip()) > 0))
            _norm = ",".join(map(str, _ids))
            payload["bot_admin_ids"] = _norm
            settings.ADMIN_IDS = _norm
        else:
            payload.pop("bot_admin_ids", None)
        await set_app_settings(session, payload)
        return {"ok": True}


class BotAdminsReq(BaseModel):
    admin_ids: list[int]


@app.get("/api/admin/bot-admins", dependencies=[Depends(verify_admin)])
async def admin_get_bot_admins():
    async with async_session() as session:
        cfg = await get_app_settings(session)
        saved = (cfg.get("bot_admin_ids") or "").strip()
        if saved:
            ids = [int(x.strip()) for x in saved.split(",") if x.strip().isdigit()]
            settings.ADMIN_IDS = ",".join(map(str, ids))
        else:
            ids = list(settings.admin_ids)
        return {"admin_ids": ids}


@app.put("/api/admin/bot-admins", dependencies=[Depends(verify_admin)])
async def admin_set_bot_admins(data: BotAdminsReq):
    ids = sorted(set(int(x) for x in data.admin_ids if int(x) > 0))
    val = ",".join(map(str, ids))
    async with async_session() as session:
        await set_app_settings(session, {"bot_admin_ids": val})
    settings.ADMIN_IDS = val
    return {"ok": True, "admin_ids": ids}


@app.get("/api/admin/tariffs", dependencies=[Depends(verify_admin)])
async def admin_tariffs():
    async with async_session() as session:
        res = await session.execute(select(Tariff).order_by(Tariff.id))
        return [
            {
                "id": t.id, "code": t.code, "name": t.name, "price": t.price,
                "days": t.days, "is_night": t.is_night, "is_active": t.is_active,
                "description": t.description or "",
            }
            for t in res.scalars().all()
        ]


@app.post("/api/admin/tariffs", dependencies=[Depends(verify_admin)])
async def admin_create_tariff(data: TariffCreateReq):
    if not data.name.strip():
        raise HTTPException(status_code=400, detail="Укажите название тарифа")
    async with async_session() as session:
        code = f"t_{int(time.time() * 1000) % 100000000}"
        t = Tariff(
            code=code, name=data.name.strip(), price=max(0, data.price),
            days=0 if data.is_night else max(1, data.days),
            is_night=data.is_night, is_active=data.is_active,
            description=data.description.strip(),
        )
        session.add(t)
        await session.commit()
        return {"ok": True, "id": t.id}


@app.put("/api/admin/tariffs/{tariff_id}", dependencies=[Depends(verify_admin)])
async def admin_update_tariff(tariff_id: int, data: TariffUpdateReq):
    async with async_session() as session:
        t = await session.get(Tariff, tariff_id)
        if not t:
            raise HTTPException(status_code=404, detail="Тариф не найден")
        t.name = data.name.strip()
        t.price = max(0, data.price)
        t.days = 0 if data.is_night else max(1, data.days)
        t.is_night = data.is_night
        t.is_active = data.is_active
        t.description = data.description.strip()
        await session.commit()
        return {"ok": True}


@app.delete("/api/admin/tariffs/{tariff_id}", dependencies=[Depends(verify_admin)])
async def admin_delete_tariff(tariff_id: int):
    async with async_session() as session:
        t = await session.get(Tariff, tariff_id)
        if not t:
            return {"ok": True}
        used = (await session.execute(select(func.count(Order.id)).where(Order.tariff_id == tariff_id))).scalar() or 0
        if used > 0:
            t.is_active = False
            await session.commit()
            return {"ok": True, "archived": True}
        await session.delete(t)
        await session.commit()
        return {"ok": True, "archived": False}


@app.get("/api/admin/promos", dependencies=[Depends(verify_admin)])
async def admin_promos():
    async with async_session() as session:
        res = await session.execute(select(PromoCode).order_by(PromoCode.id.desc()))
        return [
            {
                "id": p.id, "code": p.code, "discount_percent": p.discount_percent,
                "valid_until": p.valid_until.strftime("%Y-%m-%d") if p.valid_until else "",
                "is_active": p.is_active,
                "target_audience": p.target_audience or "all",
                "tariff_ids": parse_tariff_ids(p.tariff_ids),
            }
            for p in res.scalars().all()
        ]


@app.post("/api/admin/promos", dependencies=[Depends(verify_admin)])
async def admin_create_promo(data: PromoReq):
    code = data.code.strip().upper()
    if not code or not (1 <= data.discount_percent <= 100):
        raise HTTPException(status_code=400, detail="Укажите код и скидку от 1 до 100%")
    async with async_session() as session:
        existing = await session.execute(select(PromoCode).where(PromoCode.code == code))
        if existing.scalar_one_or_none():
            raise HTTPException(status_code=400, detail="Такой промокод уже существует")
        promo = PromoCode(
            code=code, discount_percent=data.discount_percent,
            valid_until=parse_date_end(data.valid_until),
            target_audience=data.target_audience or "all",
            tariff_ids=",".join(str(x) for x in data.tariff_ids),
        )
        session.add(promo)
        await session.commit()
        return {"ok": True, "id": promo.id}


@app.put("/api/admin/promos/{promo_id}", dependencies=[Depends(verify_admin)])
async def admin_update_promo(promo_id: int, data: PromoReq):
    code = data.code.strip().upper()
    if not code or not (1 <= data.discount_percent <= 100):
        raise HTTPException(status_code=400, detail="Укажите код и скидку от 1 до 100%")
    async with async_session() as session:
        promo = await session.get(PromoCode, promo_id)
        if not promo:
            raise HTTPException(status_code=404, detail="Промокод не найден")
        promo.code = code
        promo.discount_percent = data.discount_percent
        promo.valid_until = parse_date_end(data.valid_until)
        promo.target_audience = data.target_audience or "all"
        promo.tariff_ids = ",".join(str(x) for x in data.tariff_ids)
        await session.commit()
        return {"ok": True}


@app.delete("/api/admin/promos/{promo_id}", dependencies=[Depends(verify_admin)])
async def admin_delete_promo(promo_id: int):
    async with async_session() as session:
        promo = await session.get(PromoCode, promo_id)
        if promo:
            await session.delete(promo)
            await session.commit()
        return {"ok": True}




@app.get("/api/admin/overstay", dependencies=[Depends(verify_admin)])
async def admin_overstay_list():
    async with async_session() as session:
        daily_price = await get_daily_tariff_price(session)
        res = await session.execute(
            select(Vehicle)
            .options(selectinload(Vehicle.user))
            .where(Vehicle.overstay_status.in_(["check", "debt"]))
            .order_by(Vehicle.debt_since.asc())
        )
        items = []
        for v in res.scalars().all():
            d_days, d_amt = calc_vehicle_debt(v, daily_price)
            items.append({
                "vehicle_id": v.id,
                "phone": v.user.phone if v.user else "",
                "plate": v.plate_number,
                "spot": v.spot_label or "—",
                "valid_until": v.valid_until.strftime("%d.%m.%Y %H:%M") if v.valid_until else "—",
                "status": v.overstay_status,
                "debt_days": d_days,
                "debt_amount": d_amt,
            })
        return items


@app.post("/api/admin/overstay/{vehicle_id}/stay", dependencies=[Depends(verify_admin)])
async def admin_overstay_stay(vehicle_id: int):
    async with async_session() as session:
        try:
            await mark_vehicle_overstay(session, vehicle_id, "stay")
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@app.post("/api/admin/overstay/{vehicle_id}/left", dependencies=[Depends(verify_admin)])
async def admin_overstay_left(vehicle_id: int):
    async with async_session() as session:
        try:
            await mark_vehicle_overstay(session, vehicle_id, "left")
        except ValueError as e:
            raise HTTPException(status_code=404, detail=str(e))
    return {"ok": True}


@app.get("/api/admin/orders", dependencies=[Depends(verify_admin)])
async def admin_orders():
    async with async_session() as session:
        res = await session.execute(
            select(Order)
            .options(selectinload(Order.user).selectinload(User.vehicles), selectinload(Order.tariff), selectinload(Order.vehicle))
            .order_by(Order.id.desc())
            .limit(40)
        )
        return [
            {
                "id": o.id, "user_id": o.user_id, "phone": o.user.phone,
                "plates": o.vehicle.plate_number if o.vehicle else ", ".join([v.plate_number for v in o.user.vehicles]),
                "tariff_name": o.tariff.name, "amount": o.amount,
                "promo_code": o.promo_code, "discount_percent": o.discount_percent,
                "status": o.status, "created_at": o.created_at.strftime("%d.%m %H:%M"),
            }
            for o in res.scalars().all()
        ]


@app.post("/api/admin/orders/{order_id}/confirm", dependencies=[Depends(verify_admin)])
async def admin_confirm_order(order_id: int):
    async with async_session() as session:
        try:
            slot, user = await confirm_order(session, order_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    await notify_user_activated(user.telegram_id, slot.slot_number, slot.valid_until)
    return {"ok": True, "slot_number": slot.slot_number}


@app.post("/api/admin/orders/{order_id}/reject", dependencies=[Depends(verify_admin)])
async def admin_reject_order(order_id: int):
    async with async_session() as session:
        try:
            await reject_order(session, order_id)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
    return {"ok": True}


@app.get("/api/admin/users", dependencies=[Depends(verify_admin)])
async def admin_users():
    async with async_session() as session:
        res = await session.execute(
            select(User).options(selectinload(User.vehicles), selectinload(User.slot)).order_by(User.id.desc())
        )
        users = res.scalars().all()
        return [
            {
                "id": u.id, "phone": u.phone, "full_name": u.full_name,
                "vehicles": [
                    {
                        "id": v.id,
                        "plate": v.plate_number,
                        "model": v.car_model,
                        "spot": v.spot_label,
                        "valid_until": v.valid_until.strftime("%d.%m.%Y %H:%M") if v.valid_until else None,
                    }
                    for v in u.vehicles
                ],
                "slot": {
                    "number": u.slot.slot_number,
                    "spot": u.slot.spot_label,
                    "status": u.slot.status,
                    "valid_until": u.slot.valid_until.strftime("%d.%m.%Y %H:%M") if u.slot.valid_until else None,
                } if u.slot else None,
            }
            for u in users
        ]


@app.put("/api/admin/vehicles/{vehicle_id}/spot", dependencies=[Depends(verify_admin)])
async def admin_set_vehicle_spot(vehicle_id: int, data: SpotUpdateReq):
    async with async_session() as session:
        try:
            v = await set_vehicle_spot(session, vehicle_id, data.spot)
            return {"ok": True, "spot": v.spot_label}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@app.put("/api/admin/users/{user_id}/spot", dependencies=[Depends(verify_admin)])
async def admin_set_user_spot(user_id: int, data: SpotUpdateReq):
    async with async_session() as session:
        try:
            slot = await set_user_spot(session, user_id, data.spot)
            return {"ok": True, "spot": slot.spot_label}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/admin/grant", dependencies=[Depends(verify_admin)])
async def admin_grant(data: GrantReq):
    async with async_session() as session:
        try:
            slot = await grant_access_days(session, data.user_id, data.days)
            return {"ok": True, "slot_number": slot.slot_number}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))

class VehicleGrantReq(BaseModel):
    days: int


@app.post("/api/admin/vehicles/{vehicle_id}/grant", dependencies=[Depends(verify_admin)])
async def admin_grant_vehicle(vehicle_id: int, data: VehicleGrantReq):
    from datetime import datetime, timedelta
    async with async_session() as session:
        veh = await session.get(Vehicle, vehicle_id)
        if not veh:
            raise HTTPException(status_code=404, detail="Автомобиль не найден")
        now = datetime.utcnow()
        base = veh.valid_until if (veh.valid_until and veh.valid_until > now) else now
        veh.valid_until = base + timedelta(days=data.days)
        if hasattr(veh, "overstay_status"):
            veh.overstay_status = None
        await session.commit()
        try:
            slot = await grant_access_days(session, veh.user_id, data.days)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        # Синхронизируем общий срок слота по максимальному сроку среди всех машин клиента
        res = await session.execute(select(Vehicle).where(Vehicle.user_id == veh.user_id))
        dates = [v.valid_until for v in res.scalars().all() if v.valid_until]
        if dates and slot:
            slot.valid_until = max(dates)
            await session.commit()
        return {"ok": True, "valid_until": veh.valid_until.strftime("%d.%m.%Y %H:%M")}


@app.post("/api/admin/vehicles/{vehicle_id}/revoke", dependencies=[Depends(verify_admin)])
async def admin_revoke_vehicle(vehicle_id: int):
    from datetime import datetime
    async with async_session() as session:
        veh = await session.get(Vehicle, vehicle_id)
        if not veh:
            raise HTTPException(status_code=404, detail="Автомобиль не найден")
        uid = veh.user_id
        veh.valid_until = None
        if hasattr(veh, "overstay_status"):
            veh.overstay_status = None
        await session.commit()
        res = await session.execute(select(Vehicle).where(Vehicle.user_id == uid))
        now = datetime.utcnow()
        active_dates = [v.valid_until for v in res.scalars().all() if v.valid_until and v.valid_until > now]
        if not active_dates:
            await revoke_access(session, uid)
        else:
            slot_res = await session.execute(select(RtuSlot).where(RtuSlot.user_id == uid))
            slot = slot_res.scalar_one_or_none()
            if slot:
                slot.valid_until = max(active_dates)
                await session.commit()
        return {"ok": True}



@app.post("/api/admin/revoke", dependencies=[Depends(verify_admin)])
async def admin_revoke(data: RevokeReq):
    async with async_session() as session:
        try:
            slot = await revoke_access(session, data.user_id)
            return {"ok": True, "slot_number": slot.slot_number}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@app.get("/api/admin/extra-services", dependencies=[Depends(verify_admin)])
async def admin_get_extra_services():
    async with async_session() as session:
        res = await session.execute(select(ExtraService).order_by(ExtraService.id))
        return [{"id": x.id, "name": x.name, "price": x.price, "description": x.description or "", "is_active": x.is_active} for x in res.scalars().all()]


@app.post("/api/admin/extra-services", dependencies=[Depends(verify_admin)])
async def admin_create_extra_service(data: ExtraServiceReq):
    if not data.name.strip():
        raise HTTPException(status_code=400, detail="Укажите название услуги")
    async with async_session() as session:
        svc = ExtraService(name=data.name.strip(), price=max(0, data.price), description=data.description.strip(), is_active=data.is_active)
        session.add(svc)
        await session.commit()
        return {"ok": True, "id": svc.id}


@app.put("/api/admin/extra-services/{service_id}", dependencies=[Depends(verify_admin)])
async def admin_update_extra_service(service_id: int, data: ExtraServiceReq):
    if not data.name.strip():
        raise HTTPException(status_code=400, detail="Укажите название услуги")
    async with async_session() as session:
        svc = await session.get(ExtraService, service_id)
        if not svc:
            raise HTTPException(status_code=404, detail="Услуга не найдена")
        svc.name = data.name.strip()
        svc.price = max(0, data.price)
        svc.description = data.description.strip()
        svc.is_active = data.is_active
        await session.commit()
        return {"ok": True}


@app.delete("/api/admin/extra-services/{service_id}", dependencies=[Depends(verify_admin)])
async def admin_delete_extra_service(service_id: int):
    async with async_session() as session:
        svc = await session.get(ExtraService, service_id)
        if svc:
            await session.delete(svc)
            await session.commit()
        return {"ok": True}


@app.get("/api/admin/service-orders", dependencies=[Depends(verify_admin)])
async def admin_service_orders():
    async with async_session() as session:
        res = await session.execute(
            select(ServiceOrder)
            .options(selectinload(ServiceOrder.service))
            .order_by(ServiceOrder.id.desc())
            .limit(40)
        )
        return [
            {
                "id": o.id,
                "service_name": o.service.name if o.service else f"Услуга #{o.service_id}",
                "phone": o.phone,
                "spot": o.spot or "—",
                "comment": o.comment or "",
                "amount": o.amount,
                "status": o.status,
                "created_at": o.created_at.strftime("%d.%m %H:%M") if o.created_at else "",
            }
            for o in res.scalars().all()
        ]


@app.post("/api/admin/service-orders/{order_id}/confirm", dependencies=[Depends(verify_admin)])
async def admin_confirm_service_order(order_id: int):
    async with async_session() as session:
        o = await session.get(ServiceOrder, order_id)
        if not o:
            raise HTTPException(status_code=404, detail="Заказ не найден")
        o.status = "paid"
        await session.commit()
        return {"ok": True}


@app.post("/api/admin/service-orders/{order_id}/reject", dependencies=[Depends(verify_admin)])
async def admin_reject_service_order(order_id: int):
    async with async_session() as session:
        o = await session.get(ServiceOrder, order_id)
        if not o:
            raise HTTPException(status_code=404, detail="Заказ не найден")
        o.status = "rejected"
        await session.commit()
        return {"ok": True}


@app.get("/api/admin/queue", dependencies=[Depends(verify_admin)])
async def admin_queue():
    async with async_session() as session:
        res = await session.execute(select(SmsQueue).order_by(SmsQueue.id.desc()).limit(30))
        return [
            {
                "id": q.id, "slot": q.slot_number, "action": q.action,
                "command": q.command_text, "status": q.status,
                "error": q.error_msg, "created_at": q.created_at.strftime("%H:%M:%S"),
            }
            for q in res.scalars().all()
        ]


@app.post("/api/admin/queue/{task_id}/cancel", dependencies=[Depends(verify_admin)])
async def admin_cancel_sms(task_id: int):
    async with async_session() as session:
        q = await session.get(SmsQueue, task_id)
        if not q:
            raise HTTPException(status_code=404, detail="Задача не найдена")
        if q.status == "pending":
            q.status = "cancelled"
            await session.commit()
        return {"ok": True}


@app.post("/api/admin/queue/clear-pending", dependencies=[Depends(verify_admin)])
async def admin_clear_pending_sms():
    async with async_session() as session:
        res = await session.execute(select(SmsQueue).where(SmsQueue.status == "pending"))
        items = res.scalars().all()
        for q in items:
            q.status = "cancelled"
        await session.commit()
        return {"ok": True, "cancelled": len(items)}


@app.get("/api/internal/sms/next", dependencies=[Depends(verify_worker)])
async def worker_next():
    was_alert = worker_state["alert_sent"]
    worker_state["last_ping"] = time.time()
    worker_state["alert_sent"] = False
    if was_alert:
        await notify_admins_text("Модем RX100-R4 снова в сети.")

    async with async_session() as session:
        task = await get_next_sms_task(session)
        if not task:
            return None
        return {"id": task.id, "command_text": task.command_text}


@app.post("/api/internal/sms/{task_id}/report", dependencies=[Depends(verify_worker)])
async def worker_report(task_id: int, data: SmsReportReq):
    async with async_session() as session:
        await report_sms_task(session, task_id, data.status, data.error_msg)
        return {"ok": True}


@app.post("/api/internal/sms/incoming", dependencies=[Depends(verify_worker)])
async def worker_incoming(data: IncomingSmsReq):
    msg = f"{datetime.now().strftime('%H:%M')} от {data.sender}: {data.text}"
    worker_state["last_incoming_sms"] = msg
    logger.info(f"Входящая SMS на RX100-R4: {msg}")
    return {"ok": True}
