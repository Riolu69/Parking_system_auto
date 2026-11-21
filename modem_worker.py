import os
import re
import time
import logging
import serial
import requests
from dotenv import load_dotenv

load_dotenv()

API_URL = os.getenv("WORKER_API_URL", "http://127.0.0.1:8000/api/internal/sms")
API_TOKEN = os.getenv("WORKER_API_TOKEN", "my_secret_worker_token_123")
SERIAL_PORT = os.getenv("MODEM_PORT", "COM3")
BAUDRATE = 115200
RTU_PHONE = os.getenv("RTU_PHONE", "+79990000000")
POLL_INTERVAL = 5

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
HEADERS = {"Authorization": f"Bearer {API_TOKEN}"}


def send_sms(ser: serial.Serial, phone: str, text: str) -> tuple[bool, str]:
    try:
        ser.reset_input_buffer()
        ser.write(b"AT+CMGF=1\r")
        time.sleep(0.5)
        resp = ser.read_all().decode(errors="ignore")
        if "OK" not in resp:
            return False, f"Ошибка AT+CMGF=1: {resp.strip()}"

        ser.write(f'AT+CMGS="{phone}"\r'.encode())
        time.sleep(0.5)
        resp = ser.read_all().decode(errors="ignore")
        if ">" not in resp:
            return False, f"Модем не готов: {resp.strip()}"

        ser.write(f"{text}\x1A".encode())

        deadline = time.time() + 15
        buffer = ""
        while time.time() < deadline:
            if ser.in_waiting:
                buffer += ser.read_all().decode(errors="ignore")
                if "+CMGS:" in buffer and "OK" in buffer:
                    return True, "OK"
                if "ERROR" in buffer:
                    return False, buffer.strip()
            time.sleep(0.3)

        return False, f"Таймаут отправки. Ответ: {buffer.strip()}"
    except Exception as e:
        return False, str(e)


def read_incoming_sms(ser: serial.Serial):
    """Читает непрочитанные SMS (ответы от RTU5024) и отправляет на сервер."""
    try:
        ser.reset_input_buffer()
        ser.write(b'AT+CMGF=1\r')
        time.sleep(0.3)
        ser.read_all()
        ser.write(b'AT+CMGL="REC UNREAD"\r')
        time.sleep(1.0)
        raw = ser.read_all().decode(errors="ignore")
        if "+CMGL:" not in raw:
            return

        blocks = raw.split("+CMGL:")[1:]
        for b in blocks:
            lines = [l.strip() for l in b.strip().splitlines() if l.strip() and l.strip() != "OK"]
            if len(lines) >= 2:
                m = re.search(r'"([^"]+)"\s*,\s*"([^"]+)"', lines[0])
                sender = m.group(2) if m else "RTU5024"
                body = " ".join(lines[1:])
                logging.info(f"Входящая SMS от {sender}: {body}")
                requests.post(
                    f"{API_URL}/incoming",
                    headers=HEADERS,
                    json={"sender": sender, "text": body},
                    timeout=10,
                )

        # Очищаем прочитанные SMS, чтобы не забивать память SIM
        ser.write(b"AT+CMGD=1,2\r")
        time.sleep(0.3)
        ser.read_all()
    except Exception as e:
        logging.error(f"Ошибка чтения входящих SMS: {e}")


def main():
    logging.info(f"Запуск воркера RX100-R4. Порт: {SERIAL_PORT}, RTU: {RTU_PHONE}")
    with serial.Serial(SERIAL_PORT, BAUDRATE, timeout=2) as ser:
        while True:
            try:
                read_incoming_sms(ser)
                r = requests.get(f"{API_URL}/next", headers=HEADERS, timeout=10)
                if r.status_code == 200 and r.json():
                    task = r.json()
                    task_id = task["id"]
                    cmd = task["command_text"]
                    logging.info(f"Задача #{task_id}: отправка '{cmd}'")

                    ok, msg = send_sms(ser, RTU_PHONE, cmd)
                    status = "sent" if ok else "error"
                    logging.info(f"Задача #{task_id} -> {status} ({msg})")

                    requests.post(
                        f"{API_URL}/{task_id}/report",
                        headers=HEADERS,
                        json={"status": status, "error_msg": None if ok else msg},
                        timeout=10,
                    )
                    time.sleep(3)
                else:
                    time.sleep(POLL_INTERVAL)
            except Exception as e:
                logging.error(f"Ошибка цикла: {e}")
                time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
