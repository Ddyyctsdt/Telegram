# Telegram Join Manager V6 — Channel Requests First

نسخه V6 بر اساس تجربه واقعی کانال‌های دارای هزاران Join Request بازطراحی شده است. محور اصلی برنامه دیگر «لینک» نیست؛ محور اصلی **صف درخواست‌های خود کانال** است و مدیریت Invite Link یک بخش جداگانه محسوب می‌شود.

## فقط دو Secret

در GitHub Repository فقط این دو Secret لازم است:

- `BOT_TOKEN` — توکن BotFather
- `OWNER_ID` — آیدی عددی کسی که اجازه کنترل پنل را دارد

`API_ID`، `API_HASH` و `USER_SESSION` لازم نیست به GitHub بدهی. ورود اکانت MTProto با QR انجام می‌شود و Session رمزگذاری‌شده در `data/auth.enc` نگهداری می‌شود.

## نکته خیلی مهم درباره Owner کانال

دو مفهوم با هم فرق دارند:

- `OWNER_ID` = صاحب پنل ربات؛ فقط مشخص می‌کند چه کسی می‌تواند به ربات دستور بدهد.
- **Channel Owner** = مالک واقعی کانال در Telegram.

اکانت QR شده می‌تواند با `OWNER_ID` متفاوت باشد.

برای Admin معمولی، Telegram دسترسی به لینک‌های ساخته‌شده توسط خودش را می‌دهد. برای دیدن لینک‌های ساخته‌شده توسط سایر ادمین‌ها و گرفتن صف کامل درخواست‌های مربوط به همه لینک‌ها، اکانت QR شده باید Owner کانال باشد.

V6 این تفاوت را در داشبورد نمایش می‌دهد و اگر Session فقط Admin باشد، عملیات Global Random را به‌اشتباه اجرا نمی‌کند.

## داشبورد جدید

داشبورد کانال موارد زیر را نشان می‌دهد:

- تعداد اعضای کانال
- **کل Pending فعلی کانال** از `ChannelFull.requests_pending`
- تعداد Invite Linkهای قابل مشاهده برای Session
- تعداد لینک‌های Approval
- سطح دسترسی Session: Owner یا Admin

منو:

- `⏳ مدیریت درخواست‌های کانال`
- `🔗 مدیریت لینک‌ها`
- `➕ ساخت لینک`
- `🔄 بروزرسانی`
- `📺 کانال‌ها`
- `🔄 تغییر اکانت متصل`

## Random Approve از کل کانال

اگر Session متعلق به Owner باشد:

- 10 نفر Random
- 50 نفر Random
- 100 نفر Random
- تعداد دلخواه
- Approve All

Random از کل صف با **Reservoir Sampling** انجام می‌شود. بنابراین اگر 20,000 درخواست وجود داشته باشد، 50 نفر فقط از اولین 100 درخواست انتخاب نمی‌شوند؛ کل صف صفحه‌به‌صفحه اسکن می‌شود و نمونه تصادفی از کل لیست ساخته می‌شود، بدون اینکه لازم باشد تمام Userها همزمان در RAM ذخیره شوند.

برای عملیات Random ابتدا تأیید نهایی نمایش داده می‌شود.

## مدیریت لینک‌ها

Invite Link Manager بخش مستقلی است.

### ساخت لینک جدید

Wizard ساخت لینک:

1. عنوان مدیریتی
2. نوع ورود:
   - Approval / نیازمند تأیید مدیر
   - Direct / ورود مستقیم
3. زمان اعتبار:
   - 1 ساعت
   - 6 ساعت
   - 1 روز
   - 3 روز
   - 7 روز
   - 30 روز
   - بدون انقضا
   - ساعت دلخواه
4. برای Approval:
   - سقف Auto Approve
   - فعال/خاموش کردن گزارش یک‌دقیقه‌ای
5. برای Direct:
   - Member Limit

### بعد از ساخت لینک

برای لینک Approval می‌توان بعداً:

- Random Approve انجام داد
- تمام Pending همان لینک را Approve کرد
- سقف Auto Approve را تعیین/تغییر/خاموش کرد
- گزارش یک‌دقیقه‌ای را روشن یا خاموش کرد
- عنوان لینک را تغییر داد
- Expiration را تغییر داد
- لینک را Revoke کرد

## گزارش درخواست جدید هر یک دقیقه

برای هر لینک Approval می‌توان `گزارش یک‌دقیقه‌ای` را فعال کرد.

Bot از Update رسمی Join Request استفاده می‌کند. اگر در یک دقیقه هیچ درخواست جدیدی نیاید، **هیچ پیامی ارسال نمی‌شود**.

اگر درخواست آمده باشد، نمونه گزارش:

```text
📥 گزارش درخواست‌های جدید — یک دقیقه اخیر
کانال: Loggingol

🔗 اروپا تعداد بالا: +17 درخواست • Pending فعلی 1,517
🔗 سرعتی اروپا: +4 درخواست • Pending فعلی 846

مجموع درخواست جدید: 21 نفر
```

این شمارش بر اساس Event ورود درخواست است، بنابراین اگر Auto Approve بلافاصله کاربر را تأیید کند نیز درخواست جدید در گزارش دقیقه‌ای شمرده می‌شود.

برای دریافت این Update، BotFather bot را هم در کانال Admin کن و دسترسی Invite Users/Add Subscribers بده.

## Auto Approve

Auto Approve برای هر Approval Link مستقل است.

مثلاً:

```text
سقف Auto Approve = 500
```

ربات تا رسیدن به سقف درخواست‌های آن لینک را پردازش می‌کند. مقدار `AUTO_SCAN_SECONDS` فاصله بررسی صف را تعیین می‌کند و پیش‌فرض 20 ثانیه است.

Auto Approve را می‌توان **بعد از ساخت لینک** نیز فعال یا تغییر داد.

## OCR عدد

در ورودی‌های عددی مدیریتی می‌توان به‌جای تایپ عدد، تصویر واضح عدد را ارسال کرد. OCR عدد را استخراج می‌کند و قبل از استفاده از تو تأیید می‌گیرد.

OCR برای Login Code یا 2FA استفاده نمی‌شود. Login همچنان QR رسمی Telegram است.

## استقرار GitHub Actions

ساختار Repository:

```text
.github/
  workflows/
    run-bot.yml
data/
  state.json
bot.py
bootstrap.py
requirements.txt
README.md
.gitignore
```

در GitHub:

1. `Settings → Actions → General`
2. `Workflow permissions → Read and write permissions`
3. `Settings → Secrets and variables → Actions`
4. دو Secret بساز:
   - `BOT_TOKEN`
   - `OWNER_ID`
5. به `Actions → Telegram Join Manager V6 → Run workflow` برو.

در اولین اجرا `/start` بزن و اکانت را با QR متصل کن.

## Environmentهای Workflow

```text
APPROVAL_CONCURRENCY=15
AUTO_SCAN_SECONDS=20
AUTO_APPROVE_BATCH=100
REPORT_INTERVAL_SECONDS=60
RUN_SECONDS=20000
PERSIST_TO_GIT=true
```

`REPORT_INTERVAL_SECONDS=60` یعنی گزارش درخواست‌های جدید هر یک دقیقه Flush شود.

## مهاجرت از V5/V5.1

فرمت `auth.enc` عمداً تغییر نکرده است. اگر V5.1 هم‌اکنون با اکانتت Login است:

- **`data/auth.enc` فعلی را نگه دار و با فایل خالی/جدید جایگزین نکن.**
- بهتر است **`data/state.json` فعلی را هم نگه داری** تا کانال فعال و Policyهای قبلی حفظ شوند.
- فایل‌های اصلی V6 (`bot.py`، `bootstrap.py`، `requirements.txt`، Workflow و README) را جایگزین کن.
- Workflow را دوباره Run کن.

Session قبلی باید بدون QR مجدد قابل استفاده باشد.

`state.json` قدیمی توسط V6 مهاجرت داده می‌شود؛ فیلد گزارش دقیقه‌ای برای Policyهای قدیمی به‌صورت پیش‌فرض خاموش است. اگر Repository را از صفر می‌سازی، `data/state.json` داخل ZIP را استفاده کن.
