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
from app.database import init_db, async_session, User, SmsQueue, Tariff, Order, PromoCode, RtuSlot
from app.services import (
    normalize_phone, register_user_and_car, create_order, confirm_order, reject_order,
    grant_access_days, revoke_access, get_next_sms_task, report_sms_task,
    validate_promo_code, get_app_settings, set_app_settings, mark_order_paid_by_user,
    process_expired_slots, pop_expiring_slots, queue_open_gate, get_finance_stats
)
from app.bot import (
    bot, dp, notify_admins_order, notify_user_activated,
    notify_user_expiring, notify_admins_text, send_db_backup_to_admins, broadcast_to_users, notify_admins_support
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
                if expired_count:
                    logger.info(f"Автоудаление: {expired_count} просроченных ячеек.")

                expiring = await pop_expiring_slots(session)
                for tg_id, valid_until in expiring:
                    await notify_user_expiring(tg_id, valid_until)

            lp = worker_state["last_ping"]
            if lp is not None and (time.time() - lp) > 120 and not worker_state["alert_sent"]:
                worker_state["alert_sent"] = True
                await notify_admins_text("Внимание! Модем RX100-R4 не выходит на связь более 2 минут.")

            # Автобэкап БД раз в сутки в 03:00
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
    if authorization != f"Bearer {settings.WORKER_API_TOKEN}":
        raise HTTPException(status_code=401, detail="Invalid worker token")


def verify_admin(x_admin_password: str = Header(None)):
    if x_admin_password != settings.ADMIN_WEB_PASSWORD:
        raise HTTPException(status_code=403, detail="Invalid admin password")


def parse_date_end(date_str: str | None) -> datetime | None:
    if not date_str:
        return None
    return datetime.strptime(date_str, "%Y-%m-%d").replace(hour=23, minute=59, second=59)


class RegisterReq(BaseModel):
    phone: str
    plate_number: str
    tariff_id: int
    car_model: str | None = None
    full_name: str | None = None
    promo_code: str | None = None


class SettingsReq(BaseModel):
    sbp_phone: str
    sbp_bank: str
    sbp_recipient: str
    gate_phone: str
    bot_username: str = ""


class TariffUpdateReq(BaseModel):
    name: str
    price: int
    days: int
    is_night: bool = False
    description: str = ""


class SupportReq(BaseModel):
    text: str
    phone: str | None = None
    telegram_id: int | None = None
    full_name: str | None = None


class PromoReq(BaseModel):
    code: str
    discount_percent: int
    valid_until: str | None = None


class GrantReq(BaseModel):
    user_id: int
    days: int = 30


class RevokeReq(BaseModel):
    user_id: int


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
        return await get_app_settings(session)


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
async def api_check_promo(code: str):
    async with async_session() as session:
        try:
            promo = await validate_promo_code(session, code)
            return {
                "code": promo.code,
                "discount_percent": promo.discount_percent,
                "valid_until": promo.valid_until.strftime("%d.%m.%Y") if promo.valid_until else None,
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
        try:
            order = await create_order(session, user.id, data.tariff_id, data.promo_code)
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))
        cfg = await get_app_settings(session)
        return {
            "ok": True,
            "user_id": user.id,
            "order_id": order.id,
            "amount": order.amount,
            "promo_code": order.promo_code,
            "discount_percent": order.discount_percent,
            "phone": user.phone,
            "payment": cfg,
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
        if not user:
            raise HTTPException(status_code=404, detail="Номер не найден")
        return {
            "phone": user.phone,
            "full_name": user.full_name,
            "gate_phone": cfg["gate_phone"],
            "vehicles": [{"plate": v.plate_number, "model": v.car_model} for v in user.vehicles],
            "slot": {
                "number": user.slot.slot_number,
                "status": user.slot.status,
                "valid_until": user.slot.valid_until.strftime("%d.%m.%Y %H:%M") if user.slot.valid_until else None,
            } if user.slot else None,
        }




@app.post("/api/support")
async def api_support(data: SupportReq):
    txt = data.text.strip()
    if not txt:
        raise HTTPException(status_code=400, detail="??????? ????? ?????????")
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


@app.get("/api/admin/stats", dependencies=[Depends(verify_admin)])
async def admin_stats():
    async with async_session() as session:
        used = (await session.execute(select(func.count(RtuSlot.slot_number)).where(RtuSlot.status != "free"))).scalar() or 0
        fin = await get_finance_stats(session)
    lp = worker_state["last_ping"]
    online = lp is not None and (time.time() - lp) <= 120
    return {
        "slots_used": used,
        "slots_total": 200,
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
    w.writerow(["ID", "Телефон", "Имя", "Машины", "Ячейка", "Статус", "Действует до"])
    for u in users:
        cars = ", ".join([v.plate_number for v in u.vehicles])
        w.writerow([
            u.id, f"+{u.phone}", u.full_name or "", cars,
            u.slot.slot_number if u.slot else "",
            u.slot.status if u.slot else "",
            u.slot.valid_until.strftime("%d.%m.%Y %H:%M") if (u.slot and u.slot.valid_until) else "",
        ])
    return Response(content=("\ufeff" + buf.getvalue()).encode("utf-8"), media_type="text/csv; charset=utf-8")


@app.put("/api/admin/settings", dependencies=[Depends(verify_admin)])
async def admin_set_settings(data: SettingsReq):
    async with async_session() as session:
        await set_app_settings(session, data.model_dump())
        return {"ok": True}


@app.put("/api/admin/tariffs/{tariff_id}", dependencies=[Depends(verify_admin)])
async def admin_update_tariff(tariff_id: int, data: TariffUpdateReq):
    async with async_session() as session:
        t = await session.get(Tariff, tariff_id)
        if not t:
            raise HTTPException(status_code=404, detail="Тариф не найден")
        t.name = data.name
        t.price = data.price
        t.days = data.days
        t.is_night = data.is_night
        t.description = data.description
        await session.commit()
        return {"ok": True}


@app.get("/api/admin/promos", dependencies=[Depends(verify_admin)])
async def admin_promos():
    async with async_session() as session:
        res = await session.execute(select(PromoCode).order_by(PromoCode.id.desc()))
        return [
            {
                "id": p.id, "code": p.code, "discount_percent": p.discount_percent,
                "valid_until": p.valid_until.strftime("%Y-%m-%d") if p.valid_until else "",
                "is_active": p.is_active,
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
        promo = PromoCode(code=code, discount_percent=data.discount_percent, valid_until=parse_date_end(data.valid_until))
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


@app.get("/api/admin/orders", dependencies=[Depends(verify_admin)])
async def admin_orders():
    async with async_session() as session:
        res = await session.execute(
            select(Order)
            .options(selectinload(Order.user).selectinload(User.vehicles), selectinload(Order.tariff))
            .order_by(Order.id.desc())
            .limit(40)
        )
        return [
            {
                "id": o.id, "user_id": o.user_id, "phone": o.user.phone,
                "plates": ", ".join([v.plate_number for v in o.user.vehicles]),
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
                "vehicles": [{"plate": v.plate_number, "model": v.car_model} for v in u.vehicles],
                "slot": {
                    "number": u.slot.slot_number,
                    "status": u.slot.status,
                    "valid_until": u.slot.valid_until.strftime("%d.%m.%Y %H:%M") if u.slot.valid_until else None,
                } if u.slot else None,
            }
            for u in users
        ]


@app.post("/api/admin/grant", dependencies=[Depends(verify_admin)])
async def admin_grant(data: GrantReq):
    async with async_session() as session:
        try:
            slot = await grant_access_days(session, data.user_id, data.days)
            return {"ok": True, "slot_number": slot.slot_number}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


@app.post("/api/admin/revoke", dependencies=[Depends(verify_admin)])
async def admin_revoke(data: RevokeReq):
    async with async_session() as session:
        try:
            slot = await revoke_access(session, data.user_id)
            return {"ok": True, "slot_number": slot.slot_number}
        except ValueError as e:
            raise HTTPException(status_code=400, detail=str(e))


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

