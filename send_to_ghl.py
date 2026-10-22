import glob
import json
import os
import re
import requests

GHL_TOKEN = os.environ.get("GHL_ACCESS_TOKEN")
LOCATION_ID = os.environ.get("GHL_LOCATION_ID")
RUN_NUMBER = os.environ.get("RUN_NUMBER", "1")
REPO = os.environ.get("GITHUB_REPOSITORY", "")

if not GHL_TOKEN or not LOCATION_ID:
  print("[!] بيانات GHL غير مكتملة في Secrets.")
  exit(0)

# العثور على اسم الفيديو الناتج
videos = glob.glob("generated_videos/*.mp4")
if not videos:
  print("[!] لا يوجد فيديو لإرساله.")
  exit(0)

filename = os.path.basename(videos[0])
match = re.search(r"Surah_(\d+)_(\d+)-(\d+)_(\w+)", filename)
if match:
  surah_num, start_a, end_a, reciter = match.groups()
  title = f"تلاوة من سورة رقم {surah_num} (الآيات {start_a}-{end_a})"
else:
  title = f"مقطع قرآني خاشع #{RUN_NUMBER}"

direct_video_url = f"https://github.com/{REPO}/releases/download/video-{RUN_NUMBER}/{filename}"

headers = {
    "Authorization": f"Bearer {GHL_TOKEN}",
    "Content-Type": "application/json",
    "Version": "2021-07-28",
}

# 1. جلب معرفات الحقول المخصصة تلقائياً بالاسم
fields_url = f"https://services.leadconnectorhq.com/locations/{LOCATION_ID}/customFields"
r_fields = requests.get(fields_url, headers=headers)
url_field_id, title_field_id = None, None

if r_fields.status_code == 200:
  for f in r_fields.json().get("customFields", []):
    if f.get("name") == "Quran Video URL":
      url_field_id = f.get("id")
    elif f.get("name") == "Quran Video Title":
      title_field_id = f.get("id")

custom_fields_payload = []
if url_field_id:
  custom_fields_payload.append(
      {"id": url_field_id, "field_value": direct_video_url}
  )
if title_field_id:
  custom_fields_payload.append(
      {"id": title_field_id, "field_value": title}
  )

# 2. إنشاء / تحديث جهة الاتصال (Upsert Contact)
upsert_url = "https://services.leadconnectorhq.com/contacts/upsert"
payload = {
    "locationId": LOCATION_ID,
    "email": "automation-bot@quran.local",
    "firstName": "Quran",
    "lastName": "Automation",
    "tags": ["new_quran_video"],
    "customFields": custom_fields_payload,
}

print(f"[*] جاري إرسال البيانات إلى GoHighLevel...")
r = requests.post(upsert_url, json=payload, headers=headers, timeout=30)

if r.status_code in (200, 201):
  print(f"[✓] تم تحديث بيانات الفيديو بنجاح في GoHighLevel!")
  print(f"رابط الفيديو: {direct_video_url}")
else:
  print(f"[!] خطأ في الـ API: {r.status_code} - {r.text}")
