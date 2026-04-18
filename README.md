# نظام إدارة السنتر التعليمي — Railway Deploy

## 🚀 خطوات الرفع على Railway

### الطريقة الأولى: رفع مباشر من GitHub (الأسهل)

1. **أنشئ Repository جديد على GitHub**
   - اذهب إلى [github.com/new](https://github.com/new)
   - اسم المشروع: `center-management`
   - اجعله Private
   - اضغط **Create repository**

2. **ارفع الملفات**
   - ارفع كل الملفات الموجودة هنا للـ repository

3. **اربطه بـ Railway**
   - اذهب إلى [railway.app](https://railway.app)
   - سجّل دخول بحساب GitHub
   - اضغط **New Project** → **Deploy from GitHub repo**
   - اختر الـ repository اللي أنشأته

4. **أضف Volume لحفظ قاعدة البيانات (مهم جداً)**
   - في Railway داخل مشروعك، اضغط على الـ Service
   - اذهب إلى **Settings** → **Volumes**
   - اضغط **Add Volume**
   - Mount Path: `/data`
   - هذا يضمن إن قاعدة البيانات لا تُمسح عند إعادة النشر

5. **ضع متغيرات البيئة (اختياري)**
   - اذهب إلى **Variables** في الـ Service
   - `CENTER_NO_BROWSER` = `1` (إيقاف محاولة فتح المتصفح)

6. **ابدأ النشر**
   - Railway سيبدأ تلقائياً بناء وتشغيل التطبيق
   - بعد النشر ستحصل على رابط مثل: `https://center-xxx.railway.app`

---

### الطريقة الثانية: رفع بـ Railway CLI

```bash
# تثبيت Railway CLI
npm install -g @railway/cli

# تسجيل الدخول
railway login

# داخل مجلد المشروع
railway init
railway up
```

---

## ⚠️ ملاحظات مهمة

- **قاعدة البيانات**: تأكد من إضافة Volume على `/data` وإلا ستُفقد البيانات عند كل نشر
- **الأمان**: لا ترفع ملفات `.db` أو `center_serial_*.json` على GitHub (موجودة في `.gitignore`)
- **كلمة المرور الافتراضية**: `admin` / `1234` — غيّرها فور الدخول
- **الخطة المجانية**: Railway تتيح 500 ساعة شهرياً مجاناً

## 📁 هيكل الملفات

```
├── server.py          # السيرفر الرئيسي
├── app.html           # واجهة التطبيق
├── requirements.txt   # المكتبات المطلوبة
├── Procfile           # أمر التشغيل
├── railway.json       # إعدادات Railway
└── .gitignore         # ملفات مستثناة من Git
```
