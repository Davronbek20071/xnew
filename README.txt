# X Obunachi Multi-Userbot Tizimi

Bir nechta Telegram akkauntlarini (cheklovsiz) **Telegram Admin Bot** orqali boshqarish va **`X Obunachi [Bot]`** vazifalarini (majburiy obuna, kunlik bonus, kanallarga obuna bo‘lish va 16 kunlik avtomatik chiqish) to‘liq avtomatlashtirish tizimi.

Tizim **ikkala rejimda** ham bir xil kod bazasida ishlaydi:
1. **100% Tekin Bulutli Arxitektura (GitHub + Vercel + Supabase)** — VPS sotib olmasdan.
2. **24/7 VPS / Docker Rejimi** — Ubuntu server yoki shaxsiy kompyuterda uzluksiz fon jarayoni sifatida.

---

## Arxitektura (GitHub + Vercel + Supabase qanday birlashadi?)

Oddiy Telegram userbot (Telethon) 24/7 TCP ulanish talab qilgani uchun uni to‘g‘ridan-to‘g‘ri Vercel Serverless ichida doimiy sikl qilib ishlatib bo‘lmaydi. Ushbu loyihada muammo professional **gibrid arxitektura** bilan hal qilingan:

| Qism | Platforma | Vazifasi |
| :--- | :--- | :--- |
| **Admin Panel Boti** | **Vercel (FastAPI + Aiogram 3 Webhook)** | Telegramdan kelgan buyruqlar, interaktiv akkaunt ulash (telefon → kod → 2FA), proksi biriktirish, sozlamalar va statistikani 24/7 qabul qiladi. |
| **Ma’lumotlar Bazasi va Sessiyalar** | **Supabase (PostgreSQL)** | Barcha akkauntlar (`StringSession` Fernet shifrlash bilan), proksilar, 16 kunlik obunalar, statistika va Admin Bot FSM holatlarini saqlaydi. |
| **Userbot & Auto-Leave Worker** | **GitHub Actions (Cron)** | Har 25 daqiqada (`worker.yml`) barcha faol akkauntlarni Supabase’dan olib, vazifalarni bajaradi; har 6 soatda (`auto_leave.yml`) 16 kun to‘lgan kanallardan chiqadi. |

---

## 1-bosqich: Kerakli kalitlarni olish

1. **Telegram Admin Bot tokeni:** `@BotFather` orqali yangi bot yarating va `BOT_TOKEN` ni oling.
2. **Admin Telegram ID:** `@userinfobot` orqali o‘zingizning raqamli ID’ingizni oling (`ADMIN_IDS`).
3. **Telegram Userbot API:** [my.telegram.org](https://my.telegram.org) → **API development tools** bo‘limidan `DEFAULT_API_ID` va `DEFAULT_API_HASH` ni oling.
4. **Sessiya shifrlash kaliti (tavsiya etiladi):** Lokal terminalda quyidagi buyruqni ishlatib `SESSION_ENCRYPTION_KEY` yarating:
   ```bash
   python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
   ```

---

## 2-bosqich: Supabase’ni sozlash (Tekin)

1. [supabase.com](https://supabase.com) da yangi loyiha yarating.
2. **SQL Editor** bo‘limiga kiring, `supabase/schema.sql` fayli ichidagi kodni to‘liq nusxalab joylashtiring va **Run** tugmasini bosing.
3. **Project Settings → API** bo‘limidan quyidagilarni nusxalang:
   * `Project URL` → `SUPABASE_URL`
   * `service_role` (secret) kaliti → `SUPABASE_SERVICE_ROLE_KEY`

---

## 3-bosqich: GitHub va Vercel’ga joylash (Tekin)

1. Loyihani **Private** (yopiq) GitHub repozitoriyga yuklang (`git push`).
2. [vercel.com](https://vercel.com) ga kiring → **Add New Project** → GitHub repozitoriyni tanlang.
3. **Environment Variables** bo‘limiga `.env.example` dagi barcha o‘zgaruvchilarni kiriting:
   * `BOT_TOKEN`, `ADMIN_IDS`, `DEFAULT_API_ID`, `DEFAULT_API_HASH`
   * `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
   * `WEBHOOK_BASE_URL` (deploy bo‘lgach Vercel bergan domenni kiriting, masalan `https://my-bot.vercel.app`)
   * `WEBHOOK_SECRET` (o‘zingiz o‘ylab topgan maxfiy so‘z)
   * `SESSION_ENCRYPTION_KEY`
   * *(Ixtiyoriy — botdan qo‘lda ishga tushirish uchun)* `GITHUB_TOKEN`, `GITHUB_REPO` (`login/repo`)
4. **Deploy** tugmasini bosing.
5. Deploy tugagach, brauzerda quyidagi manzilni bir marta ochib Telegram Webhook’ni ulang:
   ```text
   https://SIZNING-DOMEN.vercel.app/api/setup-webhook?secret=SIZNING_WEBHOOK_SECRET
   ```
   Agar `{"ok": true, "webhook_url": "..."}` qaytsa — Admin Bot tayyor!

---

## 4-bosqich: GitHub Actions Worker’ni yoqish (Tekin)

1. GitHub repozitoriyda **Settings → Secrets and variables → Actions → New repository secret** bo‘limiga kiring va quyidagi secretlarni kiriting:
   * `BOT_TOKEN`, `ADMIN_IDS`, `DEFAULT_API_ID`, `DEFAULT_API_HASH`
   * `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY`
   * `SESSION_ENCRYPTION_KEY`, `DEFAULT_TARGET_BOT`
2. **Actions** bo‘limiga o‘tib, **X Obunachi Userbot Worker** va **X Obunachi Auto-Leave Worker** workflow’larini faollashtiring (`Enable workflow`).
3. **Muhim eslatma (GitHub tekin limiti):** Private repozitoriyda GitHub Actions oyiga **2 000 daqiqa** tekin beradi. Agar 6–8 ta akkaunt bilan har sikl ~2 daqiqa olsa, `*/35 * * * *` yoki `*/45 * * * *` cron oralig‘i oylik limitga bemalol sig‘adi (`.github/workflows/worker.yml` faylidan o‘zgartirish mumkin).

---

## Muqobil: Ubuntu VPS / Docker’da 24/7 ishga tushirish

Agar keyinchalik VPS serverga o‘tmoqchi bo‘lsangiz, kodni o‘zgartirish shart emas:

### Docker Compose orqali
```bash
cp .env.example .env
nano .env   # Kalitlarni to'ldiring
docker compose up -d --build
docker compose logs -f
```

### Ubuntu `systemd` orqali
```bash
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env && nano .env

sudo cp deploy/x-obunachi.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now x-obunachi
sudo journalctl -u x-obunachi -f
```

---

## Admin Botdan foydalanish

Botga `/start` yuboring:
* **➕ Akkaunt qo‘shish:** Telefon raqam → Telegramdan kelgan kod (masalan `1 2 3 4 5`) → agar bo‘lsa 2FA parol. Sessiya avtomatik Supabase’ga saqlanadi.
* **📱 Akkauntlar:** Har bir akkaunt holati (🟢/🔴/⚠️/❌), balansi, proksisi, faol kanallari soni, individual va ommaviy Start/Stop.
* **🌐 Proksilar:** `socks5://user:pass@ip:port` qo‘shish (saqlashdan oldin Telegram DC ga ulanishi jonli tekshiriladi) va akkauntga biriktirish.
* **⚙️ Sozlamalar:** Sikl oralig‘i (20–25 daqiqa), harakatlar pauzasi (3–8 soniya), maqsadli bot username’i va Auto-Leave muddati (16 kun).
* **📊 Statistika:** Barcha akkauntlar bo‘yicha kunlik va umumiy hisobot.
* **🚀 Hozir ishga tushirish:** GitHub Actions Worker’ni kutmasdan darhol ishga tushirish.

---

## Testlarni ishga tushirish

```bash
pytest -v
```
