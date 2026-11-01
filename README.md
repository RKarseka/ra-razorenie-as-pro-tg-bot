# ra-razorenie-as-pro-tg-bot

Удалённый чекин в Allsports с дежурного телефона: Telegram-бот по webhook
(serverless-стиль) → сервер дома → adb по Wi-Fi → запасной Android на зарядке.

## Архитектура

```
ты → Telegram → webhook → LXC на Proxmox (этот бот)
                              │  adb connect <phone>:5555 (LAN)
                              ▼
                    дежурный телефон дома
                      ├─ Fake GPS (com.lexa.fakegps) — координаты
                      └─ Allsports (by.allsports.holder) — чекин
```

Telegram → бот через **webhook**, наружу торчит только Cloudflare Tunnel —
VPS и белый IP не нужны.

## Как работает чекин

1. `/checkin <name>` → бот берёт координаты из SQLite (объекты управляются
   командами `/addplace`, `/places`, `/delplace`).
2. `adb shell am start-foreground-service -a com.lexa.fakegps.START
   -n com.lexa.fakegps/.FakeGPSService --es lat X --es long Y`
   — у Lexa Fake GPS есть exported-сервис с intent-API (проверено по
   декомпилу версии 2.1.2), так что GPS выставляется без единого тапа.
3. Открывается Allsports, `uiautomator2` выбирает объект из списка по гео
   и жмёт Check in — текстовые селекторы, не координаты.
4. Скриншоты каждого шага прилетают в чат; запрос ждёт подтверждения админа.

`/stop` — остановить спуфинг, `/shot` — скриншот, `/status` — связность.

## Почему реальное железо, а не эмулятор

В APK Allsports есть анти-спуф телеметрия: `isMocked`/`geo_mocked`,
`isPhysicalDevice`, `rooted`. Эмулятор палится по `isPhysicalDevice=false`;
Fake GPS на реальном телефоне ставит только `isMocked` — тот же флаг, что и
при ручном сценарии, который уже работает.

## Файлы

- `bot/` — aiogram 3 webhook-бот, SQLite-хранилище объектов, adb/uiautomator2
  драйвер устройства.
- `docs/device_setup.md` — настройка телефона (mock location, adb tcpip,
  батарея/MIUI).
- `docs/proxmox_deploy.md` — LXC, systemd, Cloudflare Tunnel, setWebhook.
- `scripts/setup_device.sh` — разовый прогон после подключения USB.

## Что нужно при первом запуске

1. Раз вручную залогиниться в Allsports на дежурном телефоне.
2. Один USB-контакт для `adb tcpip 5555` (и после каждой перезагрузки
   телефона — телефон живёт на зарядке, так что редко).
3. Одна калибровка UI-селекторов через `/shot`, если флоу чекина в реальной
   сборке приложения отличается.
