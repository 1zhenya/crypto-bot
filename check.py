import json
import os

import requests

BOT_TOKEN = os.environ["BOT_TOKEN"]
API = f"https://api.telegram.org/bot{BOT_TOKEN}"

DROP_PERCENT = 10          # на сколько процентов должен упасть курс
STATE_FILE = "state.json"  # здесь хранятся цены и номер последнего прочитанного сообщения

# Источники пробуются по очереди, пока один не ответит
SOURCES = [
    ("Coinbase", "https://api.coinbase.com/v2/prices/BTC-USD/spot",
     lambda d: float(d["data"]["amount"])),
    ("Kraken", "https://api.kraken.com/0/public/Ticker?pair=XBTUSD",
     lambda d: float(next(iter(d["result"].values()))["c"][0])),
    ("CoinGecko", "https://api.coingecko.com/api/v3/simple/price?ids=bitcoin&vs_currencies=usd",
     lambda d: float(d["bitcoin"]["usd"])),
    ("Bybit", "https://api.bybit.com/v5/market/tickers?category=spot&symbol=BTCUSDT",
     lambda d: float(d["result"]["list"][0]["lastPrice"])),
    ("Binance", "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT",
     lambda d: float(d["price"])),
]

_price_cache = {"done": False, "value": None}


def get_btc_price():
    """Курс запрашивается один раз за запуск, дальше берётся из памяти."""
    if _price_cache["done"]:
        return _price_cache["value"]
    value = None
    for name, url, parse in SOURCES:
        try:
            response = requests.get(url, timeout=10)
            response.raise_for_status()
            value = parse(response.json())
            break
        except Exception as e:
            print(f"[{name}] Ошибка: {e}")
    _price_cache["done"] = True
    _price_cache["value"] = value
    return value


def load_state():
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception as e:
            print("Не удалось прочитать state.json:", e)
    return {"offset": 0, "users": {}}


def save_state(state):
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=2)


def send(chat_id, text):
    try:
        response = requests.post(
            f"{API}/sendMessage",
            json={"chat_id": int(chat_id), "text": text},
            timeout=15,
        )
        response.raise_for_status()
        return True
    except Exception as e:
        print(f"Не удалось отправить сообщение в чат {chat_id}: {e}")
        return False


def handle_message(state, msg):
    chat_id = str(msg["chat"]["id"])
    parts = msg["text"].split()
    if not parts or not parts[0].startswith("/"):
        return

    command = parts[0].split("@")[0].lower()
    users = state["users"]

    if command == "/start":
        send(
            chat_id,
            "👋 Привет! Я твой крипто-помощник.\n\n"
            "Когда купишь биткоин, напиши мне команду:\n"
            "/set [курс в долларах] (например: /set 80000)\n\n"
            f"Я запомню эту цену. Если курс упадёт на {DROP_PERCENT}%, я сразу сообщу!\n\n"
            "Другие команды:\n"
            "/price - текущий курс BTC\n"
            "/status - что я сейчас отслеживаю\n"
            "/stop - перестать отслеживать\n\n"
            "⏳ Я проверяю сообщения раз в несколько минут, поэтому отвечаю с небольшой задержкой."
        )

    elif command == "/set":
        if len(parts) < 2:
            send(chat_id, "Ошибка! Напиши курс через пробел. Пример: /set 80000")
            return
        try:
            price = float(parts[1].replace(",", "."))
        except ValueError:
            send(chat_id, "Пожалуйста, введи корректное число. Пример: /set 80000")
            return
        if price <= 0:
            send(chat_id, "Цена должна быть больше нуля.")
            return

        users[chat_id] = {"my_price": price, "alert_triggered": False}
        target_price = price * (1 - DROP_PERCENT / 100)
        current_price = get_btc_price()
        if current_price:
            market_info = f"📊 Текущий курс на бирже прямо сейчас: ${current_price:,.2f}\n"
        else:
            market_info = "📊 Текущий курс на бирже: (не удалось получить, попробуй позже)\n"
        send(
            chat_id,
            f"✅ Цена твоей покупки зафиксирована: ${price:,.2f}\n"
            f"{market_info}"
            f"🚨 Уведомление сработает при падении до или ниже: ${target_price:,.2f} (-{DROP_PERCENT}%)"
        )

    elif command == "/price":
        current_price = get_btc_price()
        if current_price:
            send(chat_id, f"📊 Курс BTC сейчас: ${current_price:,.2f}")
        else:
            send(chat_id, "Не удалось получить курс. Попробуй позже.")

    elif command == "/status":
        data = users.get(chat_id)
        if not data:
            send(chat_id, "Ты пока ничего не отслеживаешь. Напиши /set 80000")
            return
        price = data["my_price"]
        target_price = price * (1 - DROP_PERCENT / 100)
        state_text = (
            "уже сработало ✅ (чтобы начать заново, напиши /set)"
            if data["alert_triggered"] else "жду падения ⏳"
        )
        send(
            chat_id,
            f"Цена покупки: ${price:,.2f}\n"
            f"Порог уведомления: ${target_price:,.2f}\n"
            f"Состояние: {state_text}"
        )

    elif command == "/stop":
        if users.pop(chat_id, None):
            send(chat_id, "Хорошо, больше не слежу. Чтобы начать снова, напиши /set 80000")
        else:
            send(chat_id, "Ты и так ничего не отслеживаешь.")


def process_updates(state):
    """Читает новые сообщения, которые пришли боту с прошлого запуска."""
    try:
        response = requests.get(
            f"{API}/getUpdates",
            params={"offset": state["offset"], "timeout": 0},
            timeout=15,
        )
        response.raise_for_status()
        updates = response.json().get("result", [])
    except Exception as e:
        print("Не удалось получить сообщения:", e)
        return

    print(f"Новых сообщений: {len(updates)}")
    for update in updates:
        state["offset"] = update["update_id"] + 1
        msg = update.get("message")
        if not msg or "text" not in msg:
            continue
        try:
            handle_message(state, msg)
        except Exception as e:
            print("Ошибка при обработке сообщения:", e)


def check_alerts(state):
    """Проверяет курс и отправляет уведомления тем, у кого курс упал ниже порога."""
    active = {
        chat_id: data
        for chat_id, data in state["users"].items()
        if not data["alert_triggered"]
    }
    print(f"Отслеживается чатов: {len(active)}")
    if not active:
        return

    current_price = get_btc_price()
    print(f"Курс сейчас: {current_price}")
    if not current_price:
        return

    for chat_id, data in active.items():
        my_price = data["my_price"]
        drop_threshold = my_price * (1 - DROP_PERCENT / 100)
        if current_price <= drop_threshold:
            percent_drop = (my_price - current_price) / my_price * 100
            sent = send(
                chat_id,
                "🚨 ВНИМАНИЕ! КРИПТА УПАЛА! 🚨\n\n"
                f"📉 Курс упал более чем на {DROP_PERCENT}% от твоей покупки!\n"
                f"Цена закупа: ${my_price:,.2f}\n"
                f"Текущий курс: ${current_price:,.2f} (падение на {percent_drop:.1f}%)\n\n"
                "💰 Время доставать засадный полк из заначки и докупать!"
            )
            if sent:
                data["alert_triggered"] = True
                print(f"Уведомление отправлено в чат {chat_id}")


def main():
    state = load_state()
    process_updates(state)
    save_state(state)
    check_alerts(state)
    save_state(state)


if __name__ == "__main__":
    main()
