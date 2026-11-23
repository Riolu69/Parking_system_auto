import re
from datetime import datetime, timedelta
from sqlalchemy import select, func
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import settings
from app.database import (
    User, Vehicle, RtuSlot, SmsQueue, Tariff, Order, PromoCode, AppSetting, ExtraService, ServiceOrder
)


def normalize_phone(phone: str) -> str:
    digits = re.sub(r"\D", "", phone)
    if len(digits) == 11 and digits.startswith("8"):
        digits = "7" + digits[1:]
    elif len(digits) == 10:
        digits = "7" + digits
    return digits


def normalize_plate(plate: str) -> str:
    return re.sub(r"\s+", "", plate).upper()


def calc_valid_until(tariff: Tariff, current_until: datetime | None = None) -> datetime:
    now = datetime.now()
    if tariff.is_night:
        if now.hour < 8:
            return now.replace(hour=8, minute=0, second=0, microsecond=0)
        return (now + timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)

    base = current_until if (current_until and current_until > now) else now
    return base + timedelta(days=tariff.days)


async def get_app_settings(session: AsyncSession) -> dict:
    res = await session.execute(select(AppSetting))
    data = {s.key: s.value for s in res.scalars().all()}
    raw_max = data.get("max_spots", "200")
    try:
        max_spots = max(0, int(str(raw_max).strip()))
    except ValueError:
        max_spots = 200
    return {
        "sbp_phone": data.get("sbp_phone", ""),
        "sbp_bank": data.get("sbp_bank", ""),
        "sbp_recipient": data.get("sbp_recipient", ""),
        "gate_phone": data.get("gate_phone", settings.RTU_PHONE),
        "bot_username": data.get("bot_username", ""),
        "max_spots": max_spots,
        "description_enabled": data.get("description_enabled", "0") in ("1", "true", "True", True),
        "description_text": data.get("description_text", ""),
    }


async def set_app_settings(session: AsyncSession, updates: dict):
    for k, v in updates.items():
        if v is None:
            continue
        row = await session.get(AppSetting, k)
        if row:
            row.value = str(v).strip()
        else:
            session.add(AppSetting(key=k, value=str(v).strip()))
    await session.commit()


def parse_tariff_ids(raw: str | None) -> list[int]:
    if not raw:
        return []
    return [int(x) for x in raw.split(",") if x.strip().isdigit()]


PAYMENT_SETTING_DEFAULTS = {
    "pay_provider": "manual",
    "pay_auto_renew": "0",
    "yk_shop_id": "",
    "yk_secret_key": "",
    "rk_login": "",
    "rk_pass1": "",
    "rk_pass2": "",
    "tb_terminal_key": "",
    "tb_password": "",
    "cp_public_id": "",
    "cp_api_secret": "",
}


async def get_payment_settings(session: AsyncSession) -> dict:
    res = await session.execute(select(AppSetting))
    data = {s.key: s.value for s in res.scalars().all()}
    return {k: data.get(k, v) for k, v in PAYMENT_SETTING_DEFAULTS.items()}


async def set_payment_settings(session: AsyncSession, updates: dict):
    filtered = {k: v for k, v in updates.items() if k in PAYMENT_SETTING_DEFAULTS}
    await set_app_settings(session, filtered)


async def validate_promo_code(
    session: AsyncSession,
    code: str,
    user_id: int | None = None,
    phone: str | None = None,
    tariff_id: int | None = None,
) -> PromoCode:
    norm_code = code.strip().upper()
    res = await session.execute(
        select(PromoCode).where(PromoCode.code == norm_code, PromoCode.is_active == True)
    )
    promo = res.scalar_one_or_none()
    if not promo:
        raise ValueError("Промокод не найден")
    if promo.valid_until and promo.valid_until < datetime.now():
        raise ValueError("Срок действия промокода истёк")

    allowed_tariffs = parse_tariff_ids(getattr(promo, "tariff_ids", ""))
    if tariff_id is not None and allowed_tariffs and tariff_id not in allowed_tariffs:
        raise ValueError("Промокод не действует для выбранного тарифа")

    audience = getattr(promo, "target_audience", "all") or "all"
    if audience in ("new", "existing"):
        uid = user_id
        if uid is None and phone:
            norm_p = normalize_phone(phone)
            u_res = await session.execute(select(User.id).where(User.phone == norm_p))
            uid = u_res.scalar_one_or_none()
        paid_cnt = 0
        if uid is not None:
            paid_cnt = (await session.execute(
                select(func.count(Order.id)).where(Order.user_id == uid, Order.status == "paid")
            )).scalar() or 0
        if audience == "new" and paid_cnt > 0:
            raise ValueError("Промокод действует только для новых клиентов")
        if audience == "existing" and paid_cnt == 0:
            raise ValueError("Промокод действует только для постоянных клиентов")

    return promo


async def register_user_and_car(
    session: AsyncSession,
    phone: str,
    plate_number: str | None = None,
    car_model: str | None = None,
    full_name: str | None = None,
    telegram_id: int | None = None,
) -> User:
    norm_phone = normalize_phone(phone)
    res = await session.execute(
        select(User)
        .options(selectinload(User.vehicles), selectinload(User.slot))
        .where(User.phone == norm_phone)
    )
    user = res.scalar_one_or_none()

    if not user:
        user = User(phone=norm_phone, full_name=full_name, telegram_id=telegram_id)
        session.add(user)
        await session.flush()
    else:
        if telegram_id and not user.telegram_id:
            user.telegram_id = telegram_id
        if full_name and not user.full_name:
            user.full_name = full_name

    if plate_number:
        norm_plate = normalize_plate(plate_number)
        existing = await session.execute(
            select(Vehicle).where(Vehicle.user_id == user.id, Vehicle.plate_number == norm_plate)
        )
        if not existing.scalar_one_or_none():
            session.add(Vehicle(user_id=user.id, plate_number=norm_plate, car_model=car_model))

    await session.commit()
    res = await session.execute(
        select(User)
        .options(selectinload(User.vehicles), selectinload(User.slot))
        .where(User.id == user.id)
    )
    return res.scalar_one()


async def create_order(
    session: AsyncSession,
    user_id: int,
    tariff_id: int,
    promo_code: str | None = None,
    vehicle_id: int | None = None,
) -> Order:
    tariff = await session.get(Tariff, tariff_id)
    if not tariff:
        raise ValueError("Тариф не найден")

    discount = 0
    applied_code = None
    if promo_code and promo_code.strip():
        try:
            promo = await validate_promo_code(session, promo_code, user_id=user_id, tariff_id=tariff_id)
            discount = promo.discount_percent
            applied_code = promo.code
        except ValueError:
            discount = 0
            applied_code = None

    final_amount = max(0, round(tariff.price * (100 - discount) / 100))
    order = Order(
        user_id=user_id,
        tariff_id=tariff.id,
        vehicle_id=vehicle_id,
        amount=final_amount,
        promo_code=applied_code,
        discount_percent=discount,
        status="pending",
    )
    session.add(order)
    await session.commit()
    return order


async def mark_order_paid_by_user(session: AsyncSession, order_id: int) -> Order:
    res = await session.execute(
        select(Order)
        .options(selectinload(Order.user).selectinload(User.vehicles), selectinload(Order.tariff), selectinload(Order.vehicle))
        .where(Order.id == order_id)
    )
    order = res.scalar_one_or_none()
    if not order:
        raise ValueError("Заказ не найден")
    if order.status == "pending":
        order.status = "paid_by_user"
        await session.commit()
    return order


async def reject_order(session: AsyncSession, order_id: int) -> Order:
    res = await session.execute(
        select(Order).options(selectinload(Order.user)).where(Order.id == order_id)
    )
    order = res.scalar_one_or_none()
    if not order:
        raise ValueError("Заказ не найден")
    order.status = "rejected"
    await session.commit()
    return order


async def _pick_next_free_spot(session: AsyncSession) -> str:
    res = await session.execute(select(RtuSlot.spot_label).where(RtuSlot.spot_label != None, RtuSlot.status != "free"))
    used = {str(r[0]).strip() for r in res.all() if r[0]}
    n = 1
    while str(n) in used:
        n += 1
    return str(n)


async def set_user_spot(session: AsyncSession, user_id: int, spot_label: str) -> RtuSlot:
    res = await session.execute(select(RtuSlot).where(RtuSlot.user_id == user_id))
    slot = res.scalar_one_or_none()
    if not slot:
        raise ValueError("У пользователя нет активной ячейки")
    slot.spot_label = spot_label.strip() or None
    await session.commit()
    return slot


async def create_service_order(session: AsyncSession, service_id: int, phone: str, spot: str, comment: str = "") -> ServiceOrder:
    svc = await session.get(ExtraService, service_id)
    if not svc or not svc.is_active:
        raise ValueError("Услуга не найдена")
    order = ServiceOrder(
        service_id=svc.id,
        phone=normalize_phone(phone),
        spot=(spot or "").strip(),
        comment=(comment or "").strip(),
        amount=svc.price,
        status="pending",
    )
    session.add(order)
    await session.commit()
    res = await session.execute(select(ServiceOrder).options(selectinload(ServiceOrder.service)).where(ServiceOrder.id == order.id))
    return res.scalar_one()


async def get_spots_availability(session: AsyncSession) -> dict:
    cfg = await get_app_settings(session)
    max_spots = cfg["max_spots"]
    used = (await session.execute(
        select(func.count(RtuSlot.slot_number)).where(RtuSlot.status != "free")
    )).scalar() or 0
    free_spots = max(0, max_spots - int(used))
    return {
        "max_spots": max_spots,
        "used_spots": int(used),
        "free_spots": free_spots,
    }


async def grant_access_by_tariff(session: AsyncSession, user_id: int, tariff: Tariff) -> RtuSlot:
    user = await session.get(User, user_id)
    if not user:
        raise ValueError("Пользователь не найден")

    res = await session.execute(select(RtuSlot).where(RtuSlot.user_id == user_id))
    slot = res.scalar_one_or_none()

    if slot:
        slot.valid_until = calc_valid_until(tariff, slot.valid_until)
        slot.notified_expiring = False
        if not slot.spot_label:
            slot.spot_label = await _pick_next_free_spot(session)
        if slot.status != "active":
            slot.status = "pending_add"
            cmd = f"{settings.RTU_PASSWORD}A{slot.slot_number:03d}#{user.phone}#"
            session.add(SmsQueue(slot_number=slot.slot_number, action="add", command_text=cmd))
    else:
        avail = await get_spots_availability(session)
        if avail["free_spots"] <= 0:
            raise ValueError(f"Нет свободных парковочных мест (лимит: {avail['max_spots']})")

        free_res = await session.execute(
            select(RtuSlot)
            .where(RtuSlot.status == "free", RtuSlot.slot_number <= avail["max_spots"])
            .order_by(RtuSlot.slot_number)
            .limit(1)
        )
        slot = free_res.scalar_one_or_none()
        if not slot:
            free_res = await session.execute(
                select(RtuSlot).where(RtuSlot.status == "free").order_by(RtuSlot.slot_number).limit(1)
            )
            slot = free_res.scalar_one_or_none()
        if not slot:
            raise ValueError("Нет свободных ячеек в RTU5024 (все 200 заняты)")

        slot.user_id = user.id
        slot.valid_until = calc_valid_until(tariff)
        slot.notified_expiring = False
        slot.spot_label = await _pick_next_free_spot(session)
        slot.status = "pending_add"
        cmd = f"{settings.RTU_PASSWORD}A{slot.slot_number:03d}#{user.phone}#"
        session.add(SmsQueue(slot_number=slot.slot_number, action="add", command_text=cmd))

    await session.commit()
    return slot


async def confirm_order(session: AsyncSession, order_id: int) -> tuple[RtuSlot, User]:
    res = await session.execute(
        select(Order)
        .options(selectinload(Order.tariff), selectinload(Order.user), selectinload(Order.vehicle))
        .where(Order.id == order_id)
    )
    order = res.scalar_one_or_none()
    if not order:
        raise ValueError("Заказ не найден")
    if order.status == "paid":
        raise ValueError("Заказ уже подтверждён")

    order.status = "paid"
    target_until = None
    if order.vehicle:
        order.vehicle.valid_until = calc_valid_until(order.tariff, order.vehicle.valid_until)
        target_until = order.vehicle.valid_until

    slot = await grant_access_by_tariff(session, order.user_id, order.tariff)
    if target_until:
        v_res = await session.execute(select(func.max(Vehicle.valid_until)).where(Vehicle.user_id == order.user_id))
        max_v_until = v_res.scalar()
        if max_v_until and (not slot.valid_until or max_v_until > slot.valid_until):
            slot.valid_until = max_v_until
        elif target_until and (not slot.valid_until or target_until > slot.valid_until):
            slot.valid_until = target_until
        await session.commit()

    return slot, order.user


async def grant_access_days(session: AsyncSession, user_id: int, days: int = 30) -> RtuSlot:
    dummy = Tariff(code="custom", name=f"{days} дн.", price=0, days=days, is_night=False)
    return await grant_access_by_tariff(session, user_id, dummy)


async def revoke_access(session: AsyncSession, user_id: int) -> RtuSlot:
    res = await session.execute(select(RtuSlot).where(RtuSlot.user_id == user_id))
    slot = res.scalar_one_or_none()
    if not slot:
        raise ValueError("У пользователя нет активной ячейки")

    slot.status = "pending_del"
    cmd = f"{settings.RTU_PASSWORD}A{slot.slot_number:03d}##"
    session.add(SmsQueue(slot_number=slot.slot_number, action="delete", command_text=cmd))
    await session.commit()
    return slot


async def queue_open_gate(session: AsyncSession) -> SmsQueue:
    cmd = f"{settings.RTU_PASSWORD}CC"
    task = SmsQueue(slot_number=None, action="open", command_text=cmd)
    session.add(task)
    await session.commit()
    return task


async def get_finance_stats(session: AsyncSession) -> dict:
    now = datetime.now()
    day_ago = now.replace(hour=0, minute=0, second=0, microsecond=0)
    week_ago = now - timedelta(days=7)
    month_ago = now - timedelta(days=30)

    async def sum_since(dt: datetime) -> int:
        val_orders = (await session.execute(
            select(func.coalesce(func.sum(Order.amount), 0))
            .where(Order.status == "paid", Order.created_at >= dt)
        )).scalar() or 0
        val_svc = (await session.execute(
            select(func.coalesce(func.sum(ServiceOrder.amount), 0))
            .where(ServiceOrder.status == "paid", ServiceOrder.created_at >= dt)
        )).scalar() or 0
        return int(val_orders) + int(val_svc)

    return {
        "today": await sum_since(day_ago),
        "week": await sum_since(week_ago),
        "month": await sum_since(month_ago),
    }


async def process_expired_slots(session: AsyncSession) -> int:
    now = datetime.now()
    res = await session.execute(
        select(RtuSlot).where(RtuSlot.status == "active", RtuSlot.valid_until != None, RtuSlot.valid_until <= now)
    )
    expired = res.scalars().all()
    for slot in expired:
        slot.status = "pending_del"
        cmd = f"{settings.RTU_PASSWORD}A{slot.slot_number:03d}##"
        session.add(SmsQueue(slot_number=slot.slot_number, action="delete", command_text=cmd))
    if expired:
        await session.commit()
    return len(expired)


async def pop_expiring_slots(session: AsyncSession) -> list[tuple[int, datetime]]:
    now = datetime.now()
    soon = now + timedelta(hours=24)
    res = await session.execute(
        select(RtuSlot)
        .options(selectinload(RtuSlot.user))
        .where(
            RtuSlot.status == "active",
            RtuSlot.notified_expiring == False,
            RtuSlot.valid_until != None,
            RtuSlot.valid_until > now,
            RtuSlot.valid_until <= soon,
        )
    )
    slots = res.scalars().all()
    to_notify = []
    for s in slots:
        s.notified_expiring = True
        if s.user and s.user.telegram_id:
            to_notify.append((s.user.telegram_id, s.valid_until))
    if slots:
        await session.commit()
    return to_notify


async def get_next_sms_task(session: AsyncSession) -> SmsQueue | None:
    res = await session.execute(
        select(SmsQueue).where(SmsQueue.status == "pending").order_by(SmsQueue.id).limit(1)
    )
    return res.scalar_one_or_none()


async def report_sms_task(session: AsyncSession, task_id: int, status: str, error_msg: str | None = None):
    task = await session.get(SmsQueue, task_id)
    if not task:
        return
    task.status = status
    task.error_msg = error_msg

    if task.slot_number and status == "sent":
        slot = await session.get(RtuSlot, task.slot_number)
        if slot:
            if task.action == "add":
                slot.status = "active"
            elif task.action == "delete":
                slot.status = "free"
                slot.user_id = None
                slot.valid_until = None
                slot.notified_expiring = False
                slot.spot_label = None

    await session.commit()
