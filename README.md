# ☁️ Cloud — وكيل الذكاء الاصطناعي للترمنال (Windows)

**Cloud** هو وكيل ذكاء اصطناعي متكامل يعمل من الترمنال (Windows Terminal / PowerShell) بواجهة TUI داكنة ونظيفة، يدعم العربية والإنجليزية، تنفيذه المستقل للمهام (Autonomous Agent)، التشفير المحلي للمفاتيح عبر Windows DPAPI، الذاكرة الذاتية (SQLite)، التذكيرات الذكية، والربط مع Gmail و Google Calendar و Notion و MCP.

---

## 1. التثبيت (Install)

افتح **PowerShell** داخل مجلد المشروع ونفّذ:

```powershell
cd "C:\Users\nasser\Desktop\-#Program#- Nasser\Cloud-Ai"
pip install -r requirements.txt
```

---

## 2. التشغيل (Run)

لتشغيل البرنامج مباشرة:

```powershell
python cloud.py
```

أو لتشغيله على مجلد مشروع معين:

```powershell
python cloud.py --cwd "C:\path\to\your\project"
```

---

## 3. إضافة أول مفتاح API (Add First API Key)

بعد تشغيل `Cloud`:
1. اكتب في خانة الإدخال:
   ```text
   /provider add
   ```
   أو اضغط **`F3`** ثم اختر **Add Provider (+)**.
2. اكتب اسم المزود (مثلاً: `openai`, `gemini`, `openrouter`, `groq`, `deepseek`, `anthropic`, `mistral`, `ollama` أو أي اسم مخصص).
3. الصق مفتاح الـ API الخاص بك (يُحفظ مشفراً محلياً عبر **Windows DPAPI + Keyring** في جهازك فقط ولا يُطبع في السجلات أبداً).
4. اضغط **Test & Save** لاختبار المفتاح وتفعيل الموديل تلقائياً.

---

## 4. بناء ملف التنفيذ المستقل `cloud.exe` (Build EXE)

لبناء نسخة ملف واحد `cloud.exe` عبر PyInstaller، شغّل سكربت البناء:

```powershell
.\build.bat
```

بعد انتهاء البناء ستجد الملف الجاهز في:
```text
dist\cloud.exe
```

---

## 5. أهم الاختصارات والأوامر

- **`/`** : فتح قائمة الأوامر السريعة (Command Palette)
- **`@`** : إرفاق ملف من المشروع الحالي تلقائياً (`@filename.py`)
- **`F3`** : فتح نافذة اختيار الموديل ومراقبة الحصة (Quota)
- **`Shift+Tab`** : تفعيل/إيقاف الموافقة التلقائية (`auto-approve`)
- **`Ctrl+C`** : إلغاء المهمة الجارية حالياً
- **`/reminders`** : جدولة تذكيرات باللغة الطبيعية (مثال: `ذكرني بكرة الساعة 5 اتصل على احمد` أو `remind me tomorrow at 5 to call Ahmed`)
- **`/memory` و `/forget`** : عرض أو مسح الذاكرة الشخصية في `memory.db`
- **`/integrations`** : تفعيل وربط إضافات Gmail و Google Calendar و Notion


## 6. لفتح واجه تدعم العربي اتبع الخطوات الاتية 

 1.**`cmd`** : فتح قائمة الأوامر السريعة (Command Palette)
 2 تكتب الامر هاذاداخل الملف الاصلي exe 
 3 ** ".`\dist\cloud.exe --gui`