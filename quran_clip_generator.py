import os
import re
import random
import sqlite3
import subprocess
import requests
from PIL import Image, ImageDraw, ImageFont

# --- الإعدادات الأساسية ومتغيرات البيئة ---
RECITERS = {
    "minshawi": {"id": 9, "name": "محمد صديق المنشاوي"},
    "dussary": {"id": 97, "name": "ياسر الدوسري"}
}

OUT_DIR = "generated_videos"
TEMP_DIR = "temp_assets"
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

QCOM = "https://api.quran.com/api/v4"
HEADERS = {"User-Agent": "quran-video-generator/1.0"}
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

# أقصى حد مسموح به لحجم الفيديو (100 ميجابايت)
MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024

# المتغيرات الخاصة بـ GitHub و Make
MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")

# البحث التلقائي عن مسار خط المصحف
FONT_PATH = "/usr/share/fonts/opentype/fonts-hosny-amiri/AmiriQuran.ttf"
if not os.path.exists(FONT_PATH):
    import glob
    matches = glob.glob("/usr/share/fonts/**/AmiriQuran*.ttf", recursive=True)
    if matches:
        FONT_PATH = matches[0]

# --- 1. إدارة قاعدة البيانات لمنع التكرار ---
def init_db():
    conn = sqlite3.connect("quran_clips.db")
    c = conn.cursor()
    c.execute('''CREATE TABLE IF NOT EXISTS completed_clips (
                    surah INTEGER,
                    start_ayah INTEGER,
                    end_ayah INTEGER,
                    reciter TEXT,
                    duration REAL,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    PRIMARY KEY(surah, start_ayah, end_ayah, reciter)
                )''')
    conn.commit()
    return conn

def mark_clip_done(conn, surah, start_ayah, end_ayah, reciter, duration):
    c = conn.cursor()
    c.execute("INSERT OR IGNORE INTO completed_clips (surah, start_ayah, end_ayah, reciter, duration) VALUES (?, ?, ?, ?, ?)",
              (surah, start_ayah, end_ayah, reciter, duration))
    conn.commit()

# --- 2. جلب نصوص الآيات والتوقيتات واسم السورة ---
def fetch_surah_data(surah_num, reciter_id):
    r_info = requests.get(f"{QCOM}/chapters/{surah_num}", headers=HEADERS, timeout=60)
    r_info.raise_for_status()
    surah_name_ar = r_info.json()["chapter"]["name_arabic"]

    r_text = requests.get(f"{QCOM}/quran/verses/uthmani", params={"chapter_number": surah_num}, headers=HEADERS, timeout=60)
    r_text.raise_for_status()
    verses_text = {int(v["verse_key"].split(":")[1]): v["text_uthmani"] for v in r_text.json()["verses"]}

    r_audio = requests.get(f"{QCOM}/chapter_recitations/{reciter_id}/{surah_num}", params={"segments": "true"}, headers=HEADERS, timeout=60)
    r_audio.raise_for_status()
    data = r_audio.json()["audio_file"]

    audio_url = data["audio_url"]
    timestamps = {}
    for item in data.get("timestamps", []):
        ayah = int(item["verse_key"].split(":")[1])
        timestamps[ayah] = {
            "from": item["timestamp_from"] / 1000.0,
            "to": item["timestamp_to"] / 1000.0,
            "duration": item["duration"] / 1000.0
        }

    return surah_name_ar, verses_text, timestamps, audio_url

# --- 3. اختيار المقطع غير المكرر بمرونة تامة ---
def find_next_clip(conn, surah_num, timestamps, reciter_key):
    total_ayahs = len(timestamps)
    
    c = conn.cursor()
    c.execute("SELECT MAX(end_ayah) FROM completed_clips WHERE surah=? AND reciter=?", (surah_num, reciter_key))
    row = c.fetchone()
    last_ended = row[0] if row and row[0] is not None else 0
    
    if last_ended >= total_ayahs:
        return None

    start = last_ended + 1
    end = start

    while end <= total_ayahs:
        dur = timestamps[end]["to"] - timestamps[start]["from"]
        
        if 25.0 <= dur <= 65.0:
            return start, end, dur
        
        if dur > 65.0:
            if start == end:
                return start, end, dur
            prev_dur = timestamps[end - 1]["to"] - timestamps[start]["from"]
            return start, end - 1, prev_dur
        end += 1

    rem_dur = timestamps[total_ayahs]["to"] - timestamps[start]["from"]
    if rem_dur >= 10.0:
        return start, total_ayahs, rem_dur

    return None

# --- 4. جلب فيديو طبيعي بدون بشر من Pixabay بدقة وجودة عالية ---
def get_pixabay_video(min_duration):
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key:
        print("[!] لم يتم العثور على PIXABAY_API_KEY في متغيرات البيئة.")
        return None

    queries = ["waterfall", "sea ocean", "clouds sky", "mountains", "forest trees"]
    query = random.choice(queries)
    url = "https://pixabay.com/api/videos/"
    params = {
        "key": key,
        "q": query,
        "video_type": "film",
        "per_page": 50
    }

    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
        data = r.json()
        hits = data.get("hits", [])
        
        valid_hits = [h for h in hits if h.get("duration", 0) >= 10]
        if not valid_hits:
            print(f"[!] لم يتم العثور على مقاطع مناسبة لكلمة: {query}")
            return None

        chosen = random.choice(valid_hits)
        videos = chosen.get("videos", {})
        
        target = videos.get("large") or videos.get("medium") or videos.get("small")
        if not target or not target.get("url"):
            return None

        video_url = target["url"]
        bg_path = os.path.join(TEMP_DIR, "pixabay_bg.mp4")
        print(f"[*] جاري تحميل فيديو طبيعة ({query}) من Pixabay...")

        dl_headers = {
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/115.0.0.0 Safari/537.36"
        }
        with requests.get(video_url, headers=dl_headers, stream=True, timeout=90) as stream_res:
            stream_res.raise_for_status()
            with open(bg_path, "wb") as f:
                for chunk in stream_res.iter_content(chunk_size=1024 * 512):
                    f.write(chunk)

        if os.path.exists(bg_path) and os.path.getsize(bg_path) > 100000:
            print(f"[✓] تم تحميل فيديو الخلفية بنجاح! الحجم: {os.path.getsize(bg_path)} بايت")
            return bg_path
        else:
            print("[!] الملف المحمّل تالف أو حجمه غير مناسب.")
            return None

    except Exception as e:
        print(f"[!] خطأ أثناء سحب فيديو Pixabay: {e}")
        return None

# --- 5. محرك رسم الآيات بشفافية وخط مصغر ومظلل ---
def wrap_lines(text, font, max_w):
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if font.getlength(t, direction="rtl", language="ar") <= max_w:
            cur = t
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines

def render_ayah_slide(text, ayah_num, surah_name_ar, reciter_title, out_path):
    W, H = 1080, 1920
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)

    header_font = ImageFont.truetype(FONT_PATH, 50, layout_engine=ImageFont.Layout.RAQM)
    header_text = f"سورة {surah_name_ar} | {reciter_title}"
    d.text((W // 2, 160), header_text, font=header_font, fill=(235, 195, 80, 255), anchor="ma", direction="rtl", language="ar")

    num_ar = str(ayah_num).translate(ARABIC_DIGITS)
    full_text = f"{text} \u06dd{num_ar}"

    size = 85
    while size >= 40:
        font = ImageFont.truetype(FONT_PATH, size, layout_engine=ImageFont.Layout.RAQM)
        lines = wrap_lines(full_text, font, W - 220)
        line_h = int(size * 2.0)
        if len(lines) * line_h <= 1100:
            break
        size -= 3

    y = (H - len(lines) * line_h) // 2
    for ln in lines:
        for dx, dy in [(-2, -2), (2, -2), (-2, 2), (2, 2), (0, 3)]:
            d.text((W // 2 + dx, y + dy), ln, font=font, fill=(0, 0, 0, 240), anchor="ma", direction="rtl", language="ar")
        d.text((W // 2, y), ln, font=font, fill=(255, 255, 255, 255), anchor="ma", direction="rtl", language="ar")
        y += line_h

    img.save(out_path, "PNG")

# --- 6. الإنتاج النهائي عبر FFmpeg مع ضغط الحجم وضمان عدم تجاوز 100 ميجا ---
def generate_video(surah_num=2, reciter_key="minshawi"):
    conn = init_db()
    reciter_info = RECITERS[reciter_key]
    
    print(f"[*] جلب بيانات سورة {surah_num} للقارئ {reciter_info['name']}...")
    surah_name_ar, verses_text, timestamps, audio_url = fetch_surah_data(surah_num, reciter_info["id"])

    clip_info = find_next_clip(conn, surah_num, timestamps, reciter_key)
    if not clip_info:
        print("[!] لا توجد آيات جديدة مؤهلة للإنتاج في هذه السورة.")
        return None, None

    start_ayah, end_ayah, duration = clip_info
    print(f"[+] سورة {surah_name_ar}: من الآية {start_ayah} إلى {end_ayah} (المدة: {duration:.2f} ثانية)")

    # 1. تحميل وقص الصوت
    full_audio_path = os.path.join(TEMP_DIR, f"full_{surah_num}_{reciter_key}.mp3")
    if not os.path.exists(full_audio_path):
        r = requests.get(audio_url, headers=HEADERS, timeout=120)
        with open(full_audio_path, "wb") as f:
            f.write(r.content)

    t_start = timestamps[start_ayah]["from"]
    t_end = timestamps[end_ayah]["to"]
    cut_audio = os.path.join(TEMP_DIR, "cut.mp3")
    subprocess.run(["ffmpeg", "-y", "-ss", str(t_start), "-to", str(t_end), "-i", full_audio_path, "-acodec", "copy", cut_audio], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    # 2. توليد صور الآيات وتحديد أوقات الظهور
    slide_files = []
    for ayah in range(start_ayah, end_ayah + 1):
        slide_path = os.path.join(TEMP_DIR, f"slide_{ayah}.png")
        render_ayah_slide(verses_text[ayah], ayah, surah_name_ar, reciter_info["name"], slide_path)
        a_from = timestamps[ayah]["from"] - t_start
        a_to = timestamps[ayah]["to"] - t_start
        slide_files.append((slide_path, a_from, a_to))

    # 3. إعداد الخلفية وعزل أي صوت فيها نهائياً
    bg_video = get_pixabay_video(duration)
    inputs = []

    if bg_video and os.path.exists(bg_video):
        inputs.extend(["-stream_loop", "-1", "-an", "-i", bg_video])
        base_filter = "[0:v]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,drawbox=t=fill:c=black@0.45[bg];"
        last_label = "[bg]"
    else:
        inputs.extend(["-f", "lavfi", "-i", f"color=c=0x08131d:s=1080x1920:d={duration}"])
        base_filter = ""
        last_label = "[0:v]"

    # 4. بناء حركة Fade In / Fade Out تدريجية لكل آية
    fade_d = 0.4
    filter_chains = base_filter
    idx = 1

    for path, a_from, a_to in slide_files:
        inputs.extend(["-loop", "1", "-t", str(duration), "-i", path])
        f_in = a_from
        f_out = max(a_from, a_to - fade_d)

        slide_faded = f"[slide_faded_{idx}]"
        filter_chains += (
            f"[{idx}:v]format=yuva420p,"
            f"fade=t=in:st={f_in}:d={fade_d}:alpha=1,"
            f"fade=t=out:st={f_out}:d={fade_d}:alpha=1{slide_faded};"
        )
        
        next_label = f"[v_mix_{idx}]"
        filter_chains += f"{last_label}{slide_faded}overlay=0:0:enable='between(t,{a_from},{a_to})'{next_label};"
        last_label = next_label
        idx += 1

    out_file = os.path.join(OUT_DIR, f"Surah_{surah_num}_{start_ayah}-{end_ayah}_{reciter_key}.mp4")

    # إعدادات ضغط عالية الكفاءة (تضمن حجماً صغيراً بحدود 10 ميجا وجودة 1080p عالية)
    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-i", cut_audio,
        "-filter_complex", filter_chains.rstrip(";"),
        "-map", last_label,
        "-map", f"{idx}:a",
        "-c:v", "libx264",
        "-crf", "28",
        "-preset", "veryfast",
        "-b:v", "1500k",
        "-maxrate", "2000k",
        "-bufsize", "4000k",
        "-pix_fmt", "yuv420p",
        "-c:a", "aac",
        "-b:a", "96k",
        "-t", str(duration),
        out_file
    ]

    print("[*] جاري المعالجة وتصدير الفيديو النهائي...")
    subprocess.run(cmd, check=True)

    # التحقق من أن حجم الملف لا يتجاوز 100 ميجابايت
    actual_size = os.path.getsize(out_file)
    size_mb = actual_size / (1024 * 1024)
    print(f"[*] حجم الفيديو النهائي: {size_mb:.2f} ميجابايت")

    if actual_size > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"حجم الفيديو ({size_mb:.2f} MB) تجاوز الحد الأقصى المسموح به (100 MB)!")

    mark_clip_done(conn, surah_num, start_ayah, end_ayah, reciter_key, duration)
    print(f"[✓] تم حفظ الفيديو بنجاح: {out_file}")

    clip_title = f"سورة {surah_name_ar} (الآيات {start_ayah}-{end_ayah}) | القارئ {reciter_info['name']} #قرآن #shorts"
    return out_file, clip_title

# --- 7. رفع الفيديو إلى GitHub Releases وإشعار Make.com ---
def upload_to_github_release(video_path, tag_name="latest"):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        print("[!] بيانات GitHub Token أو المستودع غير متوفرة لرفع الـ Release.")
        return None

    file_name = os.path.basename(video_path)
    print(f"[*] جاري رفع الفيديو {file_name} إلى GitHub Releases...")

    headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Accept": "application/vnd.github+json"
    }

    release_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/tags/{tag_name}"
    res = requests.get(release_url, headers=headers)
    
    if res.status_code == 200:
        release_id = res.json()["id"]
        for asset in res.json().get("assets", []):
            if asset["name"] == file_name:
                requests.delete(asset["url"], headers=headers)
    else:
        create_url = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases"
        data = {"tag_name": tag_name, "name": "Latest Quran Clip", "draft": False, "prerelease": False}
        res_create = requests.post(create_url, headers=headers, json=data)
        res_create.raise_for_status()
        release_id = res_create.json()["id"]

    upload_url = f"https://uploads.github.com/repos/{GITHUB_REPOSITORY}/releases/{release_id}/assets?name={file_name}"
    upload_headers = {
        "Authorization": f"Bearer {GITHUB_TOKEN}",
        "Content-Type": "video/mp4"
    }
    
    with open(video_path, "rb") as f:
        upload_res = requests.post(upload_url, headers=upload_headers, data=f)
        upload_res.raise_for_status()
        
    direct_url = upload_res.json()["browser_download_url"]
    print(f"[✓] تم رفع الفيديو بنجاح: {direct_url}")
    return direct_url

def notify_make(video_url, video_title):
    if not MAKE_WEBHOOK_URL:
        print("[!] تنبيه: لم يتم العثور على MAKE_WEBHOOK_URL في Secrets.")
        return

    payload = {
        "video_url": video_url,
        "title": video_title
    }
    
    print("[*] إرسال بيانات النشر إلى Make.com...")
    res = requests.post(MAKE_WEBHOOK_URL, json=payload)
    if res.status_code in [200, 204]:
        print("[✓] استلم Make.com الطلب بنجاح وسيبدأ النشر الآن!")
    else:
        print(f"[!] خطأ أثناء إرسال البيانات إلى Make: {res.status_code} - {res.text}")

if __name__ == "__main__":
    # تشغيل التوليد
    video_path, title = generate_video(surah_num=2, reciter_key="minshawi")
    
    if video_path and title:
        # الرفع والحصول على الرابط المباشر
        direct_link = upload_to_github_release(video_path)
        
        # إرسال الرابط للـ Webhook
        if direct_link:
            notify_make(direct_link, title)
