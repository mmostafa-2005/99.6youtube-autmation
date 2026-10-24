#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quran short-video generator (نسخة محسّنة + قراء إضافيين)

المتطلبات على GitHub Actions:
    sudo apt-get install -y ffmpeg fonts-hosny-amiri libraqm0 libfribidi0 libharfbuzz0b
    pip install requests pillow

متغيرات بيئة اختيارية:
    RECITER         مفتاح القارئ من القاموس RECITERS (الافتراضي: minshawi)
    SURAH           رقم سورة ثابت (لو مش محدد بيكمل تلقائياً من آخر مقطع ويتنقل للسورة التالية)
    START_SURAH     أول سورة تبدأ بها لو قاعدة البيانات فاضية (الافتراضي: 2)
    FONT_PATH       مسار خط بديل
    PROGRESS_MODE   shared (الافتراضي) | per_reciter
                    shared      : الموضع واحد لكل القراء. لو ما حددتش SURAH، بيكمل بعد آخر مقطع
                                  اتسجل في الداتا بيز (أي قارئ)، والقارئ بيغيّر الصوت بس.
                    per_reciter : لكل قارئ تقدمه المستقل (كل قارئ بيبدأ من START_SURAH).
    CHECK_RECITERS  لو = 1: يفحص كل القراء المعرّفين ويطبع النتيجة فقط (من غير إنتاج فيديو
                    ومن غير ما يلمس قاعدة البيانات)

ملاحظة عن القراء: كل قارئ له "expect" = كلمات لازم تظهر في رابط الصوت اللي رجّعه الـ API.
لو الـ ID طلع لقارئ تاني أو أسلوب تاني (مرتل/مجوّد) السكريبت يرفض ويوقف بدل ما ينشر
مقطع باسم قارئ غلط.
"""
import glob
import math
import os
import random
import shutil
import sqlite3
import subprocess
import sys
import traceback

import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont, features
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------------------------------------------------------------------
# الإعدادات
# ----------------------------------------------------------------------------
# ids    : الـ IDs المحتملة للقارئ في Quran.com (chapter reciters). بنجرّبهم بالترتيب
#          ونقبل أول واحد رابط صوته يطابق "expect".
# expect : قايمة مجموعات؛ لازم من كل مجموعة كلمة على الأقل تظهر في رابط الصوت
#          (المقارنة بدون حروف خاصة: "yasser_ad-dussary" => "yasseraddussary").
# reject : (اختياري) كلمات لو ظهرت في الرابط نرفض الـ ID ده.
# الترتيب الظاهر ده بس للعرض؛ الاختيار الفعلي بيتم بمتغير RECITER.
_ABDULBASIT = ("abdulbaset", "abdulbasit", "abdulbasid", "abdulsamad")
RECITERS = {
    # --- متأكد منهم من الفحص اللي اتعمل ---
    "minshawi": {"ids": [9], "name": "محمد صديق المنشاوي",
                 "expect": [("minshawi", "menshawi", "minshawy"), ("murattal",)]},
    "dussary": {"ids": [97], "name": "ياسر الدوسري",
                "expect": [("dussary", "dosari", "dosary", "dossari")]},
    # --- جداد: بيتحققوا وقت التشغيل (شغّل CHECK_RECITERS=1 الأول) ---
    "abdulbasit": {"ids": list(range(1, 25)), "name": "عبد الباسط عبد الصمد",
                   "expect": [_ABDULBASIT, ("murattal",)]},
    "abdulbasit_mujawwad": {"ids": list(range(1, 25)), "name": "عبد الباسط عبد الصمد (مجوّد)",
                            "expect": [_ABDULBASIT, ("mujawwad", "mujawad")]},
    "alafasy": {"ids": list(range(1, 25)), "name": "مشاري راشد العفاسي",
                "expect": [("afasy", "alafasy", "afassy")]},
    "sudais": {"ids": list(range(1, 25)), "name": "عبد الرحمن السديس",
               "expect": [("sudais", "sudays", "sodais")]},
    "husary": {"ids": list(range(1, 25)), "name": "محمود خليل الحصري",
               "expect": [("husary", "husari", "hussary")],
               "reject": ("muallim", "mujawwad")},
    "shatri": {"ids": list(range(1, 25)), "name": "أبو بكر الشاطري",
               "expect": [("shatri", "shaatree", "shatree")]},
    "rifai": {"ids": list(range(1, 25)), "name": "هاني الرفاعي",
              "expect": [("rifai", "rifaai", "refai")]},
    "shuraym": {"ids": list(range(1, 25)), "name": "سعود الشريم",
                "expect": [("shuraym", "shuraim", "shuraym")]},
}
_RESOLVED = {}   # reciter_key -> id اللي اتأكدنا منه

OUT_DIR = "generated_videos"
TEMP_DIR = "temp_assets"
DB_PATH = "quran_clips.db"
os.makedirs(OUT_DIR, exist_ok=True)
os.makedirs(TEMP_DIR, exist_ok=True)

QCOM = "https://api.quran.com/api/v4"
HEADERS = {"User-Agent": "quran-video-generator/2.1"}
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

W, H = 1080, 1920
FPS = 30
MIN_CLIP_SEC, MAX_CLIP_SEC = 25.0, 65.0

PROGRESS_MODE = os.environ.get("PROGRESS_MODE", "shared").strip().lower()

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024      # حد عام
TELEGRAM_LIMIT_BYTES = 48 * 1024 * 1024      # حد Bot API هو 50MB

MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

# علامات الوقف والرموز القرآنية التي تأتي أحياناً ككلمة منفصلة
STOP_MARKS = "\u06d6\u06d7\u06d8\u06d9\u06da\u06db\u06dc"
STANDALONE_MARKS = set(STOP_MARKS) | {"\u06de", "\u06e9"}
PHRASE_BREAK_MARKS = set("\u06d7\u06d8\u06da\u06db")  # قلى، م، ج، ∴ → أماكن مناسبة لبدء شريحة جديدة


def make_session():
    s = requests.Session()
    retry = Retry(total=5, backoff_factor=1.5,
                  status_forcelist=[429, 500, 502, 503, 504], allowed_methods=["GET"])
    s.mount("https://", HTTPAdapter(max_retries=retry))
    s.headers.update(HEADERS)
    return s


HTTP = make_session()


# ----------------------------------------------------------------------------
# أدوات مساعدة
# ----------------------------------------------------------------------------
def find_font():
    candidates = [
        os.environ.get("FONT_PATH"),
        "/usr/share/fonts/opentype/fonts-hosny-amiri/AmiriQuran.ttf",
        "/usr/share/fonts/truetype/fonts-hosny-amiri/AmiriQuran.ttf",
    ]
    for pattern in ("/usr/share/fonts/**/AmiriQuran*.ttf", "/usr/share/fonts/**/Amiri*.ttf"):
        candidates += glob.glob(pattern, recursive=True)
    for c in candidates:
        if c and os.path.exists(c):
            return c
    raise RuntimeError("لم يتم العثور على خط المصحف. ثبّت: sudo apt-get install -y fonts-hosny-amiri")


def check_environment():
    problems = []
    if not features.check("raqm"):
        problems.append("Pillow بدون دعم RAQM (الحروف العربية هتطلع مقطّعة). "
                        "ثبّت: sudo apt-get install -y libraqm0 libfribidi0 libharfbuzz0b")
    for tool in ("ffmpeg", "ffprobe"):
        if not shutil.which(tool):
            problems.append(f"{tool} غير مثبت")
    if problems:
        raise RuntimeError("مشاكل في بيئة التشغيل:\n- " + "\n- ".join(problems))


def run(cmd, what):
    p = subprocess.run(cmd, capture_output=True, text=True)
    if p.returncode != 0:
        raise RuntimeError(f"فشل {what}:\n{p.stderr[-3000:]}")
    return p


def probe_duration(path):
    out = run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
               "-of", "default=nw=1:nk=1", path], "ffprobe").stdout.strip()
    return float(out)


def has_video_stream(path):
    p = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0",
                        "-show_entries", "stream=codec_type", "-of", "csv=p=0", path],
                       capture_output=True, text=True)
    return p.returncode == 0 and "video" in p.stdout


# ----------------------------------------------------------------------------
# 1. قاعدة البيانات (بدون أي تغيير في المنطق)
# ----------------------------------------------------------------------------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    conn.execute('''CREATE TABLE IF NOT EXISTS completed_clips (
                        surah INTEGER, start_ayah INTEGER, end_ayah INTEGER,
                        reciter TEXT, duration REAL,
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                        PRIMARY KEY(surah, start_ayah, end_ayah, reciter))''')
    conn.commit()
    return conn


def mark_clip_done(conn, surah, start_ayah, end_ayah, reciter, duration):
    conn.execute("INSERT OR IGNORE INTO completed_clips (surah, start_ayah, end_ayah, reciter, duration) "
                 "VALUES (?, ?, ?, ?, ?)", (surah, start_ayah, end_ayah, reciter, duration))
    conn.commit()


def resume_surah(conn, reciter_key, default):
    """السورة اللي وقفنا عندها: آخر مقطع اتسجل (أي قارئ لو shared، أو القارئ ده لو per_reciter)."""
    if PROGRESS_MODE == "per_reciter":
        row = conn.execute("SELECT surah FROM completed_clips WHERE reciter=? ORDER BY rowid DESC LIMIT 1",
                           (reciter_key,)).fetchone()
    else:
        row = conn.execute("SELECT surah FROM completed_clips ORDER BY rowid DESC LIMIT 1").fetchone()
    return row[0] if row else default


# ----------------------------------------------------------------------------
# 2. جلب النصوص والتوقيتات
# ----------------------------------------------------------------------------
def _norm(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def url_matches(url, cfg):
    """يتأكد إن رابط الصوت فعلاً للقارئ/الأسلوب المطلوب."""
    u = _norm(url)
    if any(_norm(x) in u for x in cfg.get("reject", ())):
        return False
    return all(any(_norm(alt) in u for alt in group) for group in cfg["expect"])


def resolve_chapter_audio(reciter_key, surah_num):
    """يرجّع (id, audio_file) للقارئ المطلوب بعد التأكد إن الرابط يطابق expect."""
    cfg = RECITERS[reciter_key]
    candidates = [_RESOLVED[reciter_key]] if reciter_key in _RESOLVED else cfg["ids"]
    seen = []
    for rid in candidates:
        r = HTTP.get(f"{QCOM}/chapter_recitations/{rid}/{surah_num}",
                     params={"segments": "true"}, timeout=60)
        if r.status_code in (400, 404, 422):     # ID مش موجود: نكمل
            continue
        r.raise_for_status()
        data = r.json()["audio_file"]
        url = data.get("audio_url", "")
        if url_matches(url, cfg):
            _RESOLVED[reciter_key] = rid
            print(f"[*] {reciter_key}: id={rid} | {url}")
            return rid, data
        seen.append(f"id={rid}: {url}")
    raise RuntimeError(
        f"مفيش ID يطابق القارئ '{reciter_key}' ({cfg['name']}). "
        f"الروابط اللي اتفحصت:\n  " + "\n  ".join(seen[:30]) +
        "\nعدّل expect/ids في RECITERS حسب الاسم الصح في الرابط.")


def parse_timestamps(data):
    timestamps = {}
    for item in data.get("timestamps", []) or []:
        ayah = int(item["verse_key"].split(":")[1])
        timestamps[ayah] = {
            "from": item["timestamp_from"] / 1000.0,
            "to": item["timestamp_to"] / 1000.0,
            "segments": item.get("segments"),
        }
    return timestamps


def _ts_context(timestamps, ayah, span=2):
    rows = []
    for a in range(max(1, ayah - span), ayah + span + 1):
        if a in timestamps:
            t = timestamps[a]
            rows.append(f"آية {a}: من {t['from']:.2f} إلى {t['to']:.2f}")
    return " | ".join(rows)


def validate_timestamps(timestamps, tolerance=0.3):
    """بعض القراء توقيتاتهم غير سليمة (مدة سالبة/تداخل). نرفضها بدل ما نقص صوت غلط."""
    prev_end = 0.0
    for ayah in sorted(timestamps):
        t = timestamps[ayah]
        if t["to"] <= t["from"]:
            raise RuntimeError(f"توقيت غير منطقي للآية {ayah}. حواليها: {_ts_context(timestamps, ayah)}")
        if t["from"] < prev_end - tolerance:
            raise RuntimeError(f"توقيت الآية {ayah} بيتداخل مع الآية اللي قبلها "
                               f"(بداية {t['from']:.2f} ث قبل نهاية السابقة {prev_end:.2f} ث). "
                               f"حواليها: {_ts_context(timestamps, ayah)}")
        prev_end = t["to"]


def fetch_surah_data(surah_num, reciter_key):
    r = HTTP.get(f"{QCOM}/chapters/{surah_num}", timeout=60)
    r.raise_for_status()
    surah_name_ar = r.json()["chapter"]["name_arabic"]

    r = HTTP.get(f"{QCOM}/quran/verses/uthmani", params={"chapter_number": surah_num}, timeout=60)
    r.raise_for_status()
    verses_text = {int(v["verse_key"].split(":")[1]): v["text_uthmani"] for v in r.json()["verses"]}

    rid, data = resolve_chapter_audio(reciter_key, surah_num)
    timestamps = parse_timestamps(data)

    missing = [a for a in verses_text if a not in timestamps]
    if not timestamps or missing:
        raise RuntimeError(f"توقيتات الآيات ناقصة لهذا القارئ/السورة (ناقص: {missing[:5]}...)")

    return surah_name_ar, verses_text, timestamps, data["audio_url"], rid


def check_reciters():
    """وضع الفحص: يتأكد من كل القراء من غير ما ينتج فيديو."""
    print("[*] فحص القراء المعرّفين (سورة 112 كعينة)...")
    for key, cfg in RECITERS.items():
        try:
            rid, data = resolve_chapter_audio(key, 112)
            ts = parse_timestamps(data)
            validate_timestamps(ts)
            if len(ts) != 4:
                raise RuntimeError(f"عدد التوقيتات {len(ts)} بدل 4")
            print(f"[OK]   {key} ({cfg['name']}): id={rid} | {data['audio_url']}")
        except Exception as e:
            print(f"[FAIL] {key} ({cfg['name']}): {e}")
    return 0


# ----------------------------------------------------------------------------
# 3. اختيار المقطع التالي (بدون أي تغيير)
# ----------------------------------------------------------------------------
def find_next_clip(conn, surah_num, timestamps, reciter_key):
    total = len(timestamps)
    if PROGRESS_MODE == "per_reciter":
        row = conn.execute("SELECT MAX(end_ayah) FROM completed_clips WHERE surah=? AND reciter=?",
                           (surah_num, reciter_key)).fetchone()
    else:
        row = conn.execute("SELECT MAX(end_ayah) FROM completed_clips WHERE surah=?",
                           (surah_num,)).fetchone()
    last_ended = row[0] if row and row[0] is not None else 0
    if last_ended >= total:
        return None

    start = last_ended + 1
    end = start
    while end <= total:
        dur = timestamps[end]["to"] - timestamps[start]["from"]
        if MIN_CLIP_SEC <= dur <= MAX_CLIP_SEC:
            return start, end, dur
        if dur > MAX_CLIP_SEC:
            if start == end:
                return start, end, dur          # آية طويلة واحدة
            return start, end - 1, timestamps[end - 1]["to"] - timestamps[start]["from"]
        end += 1

    # وصلنا لنهاية السورة: ننتج الباقي مهما كان قصيراً حتى لا تضيع آخر الآيات
    return start, total, timestamps[total]["to"] - timestamps[start]["from"]


# ----------------------------------------------------------------------------
# 4. فيديو الخلفية
# ----------------------------------------------------------------------------
BAD_TAGS = {"person", "people", "woman", "women", "man", "men", "girl", "boy", "face",
            "child", "children", "couple", "portrait", "model", "family"}

CATEGORIES = [
    ["sunset ocean", "golden hour landscape", "sunrise horizon clouds"],
    ["milky way night sky", "aurora borealis", "river stream nature"],
    ["autumn forest aerial", "clouds blue sky timelapse", "tropical sea aerial"],
    ["green mountains valley", "waterfall nature", "starry night"],
]


def pick_variant(videos):
    """يفضّل أعلى دقة بحجم معقول (4K يعطي حدة أفضل عند القص الرأسي)."""
    options = [v for v in videos.values() if isinstance(v, dict) and v.get("url")]
    if not options:
        return None
    ok = [v for v in options if v.get("size", 0) <= 150 * 1024 * 1024]
    pool = ok or options
    return max(pool, key=lambda v: (v.get("height", 0), -v.get("size", 0)))


def get_pixabay_video():
    key = os.environ.get("PIXABAY_API_KEY", "").strip()
    if not key:
        print("[!] PIXABAY_API_KEY غير موجود، سيتم استخدام خلفية بسيطة.")
        return None

    bg_path = os.path.join(TEMP_DIR, "pixabay_bg.mp4")
    tmp_path = bg_path + ".part"
    dl_headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                                "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"}

    queries = random.sample(random.choice(CATEGORIES), 3)
    for query in queries:
        try:
            r = HTTP.get("https://pixabay.com/api/videos/",
                         params={"key": key, "q": query, "per_page": 50, "safesearch": "true",
                                 "min_width": 1920}, timeout=30)
            r.raise_for_status()
            hits = r.json().get("hits", [])
            hits = [h for h in hits
                    if h.get("duration", 0) >= 8
                    and not (set(t.strip() for t in h.get("tags", "").lower().replace(" ", ",").split(","))
                             & BAD_TAGS)]
            if not hits:
                continue
            # نفضّل المقاطع الأطول لتقليل ظهور نقطة التكرار
            long_hits = [h for h in hits if h.get("duration", 0) >= 15] or hits
            random.shuffle(long_hits)

            for chosen in long_hits[:3]:
                variant = pick_variant(chosen.get("videos", {}))
                if not variant:
                    continue
                print(f"[*] تحميل خلفية ({query}) بدقة {variant.get('width')}x{variant.get('height')} ...")
                with HTTP.get(variant["url"], headers=dl_headers, stream=True, timeout=120) as s:
                    s.raise_for_status()
                    with open(tmp_path, "wb") as f:
                        for chunk in s.iter_content(chunk_size=1024 * 512):
                            f.write(chunk)
                if os.path.getsize(tmp_path) > 100_000 and has_video_stream(tmp_path):
                    os.replace(tmp_path, bg_path)
                    print(f"[✓] تم تحميل الخلفية ({os.path.getsize(bg_path) / 1024 / 1024:.1f} MB)")
                    return bg_path
        except Exception as e:
            print(f"[!] خطأ أثناء جلب الخلفية ({query}): {e}")
            continue

    print("[!] تعذر جلب خلفية من Pixabay، سيتم استخدام خلفية بسيطة.")
    return None


# ----------------------------------------------------------------------------
# 5. تقسيم النص وتوقيت الشرائح
# ----------------------------------------------------------------------------
def split_words(text):
    """يقسم الآية لكلمات ويلصق علامات الوقف المنفصلة بالكلمة السابقة."""
    words = []
    for tok in text.split():
        if words and all(ch in STANDALONE_MARKS for ch in tok):
            words[-1] = f"{words[-1]} {tok}"
        else:
            words.append(tok)
    return words


def plan_chunks(words, max_w=4):
    """يرجع قائمة (بداية, نهاية) لكل شريحة: يقطع عند علامات الوقف ثم يوزع الكلمات بالتساوي (≤4)."""
    n = len(words)
    if n == 0:
        return []
    phrases, s = [], 0
    for i, w in enumerate(words):
        if w and w[-1] in PHRASE_BREAK_MARKS and i < n - 1:
            phrases.append([s, i + 1])
            s = i + 1
    phrases.append([s, n])

    merged = []
    for p in phrases:                      # دمج العبارات القصيرة جداً (كلمة واحدة)
        if merged and (p[1] - p[0] < 2 or merged[-1][1] - merged[-1][0] < 2):
            merged[-1][1] = p[1]
        else:
            merged.append(p)

    out = []
    for a, b in merged:
        length = b - a
        k = math.ceil(length / max_w)
        base, extra = divmod(length, k)
        pos = a
        for j in range(k):
            size = base + (1 if j < extra else 0)
            out.append((pos, pos + size))
            pos += size
    return out


def word_starts(ayah_ts, n_words):
    """يستخرج توقيت بداية كل كلمة من segments؛ يرجع None لو البيانات غير موثوقة."""
    raw = ayah_ts.get("segments")
    if not raw:
        return None
    a_from, a_to = ayah_ts["from"], ayah_ts["to"]
    starts = {}
    try:
        for s in raw:
            if len(s) >= 4:
                pos, f = int(s[1]), float(s[2])
            elif len(s) == 3:
                pos, f = int(s[0]), float(s[1])
            else:
                continue
            starts.setdefault(pos, f / 1000.0)
        arr = [starts[i] for i in range(1, n_words + 1)]
    except (KeyError, ValueError, TypeError):
        return None

    if arr[0] < a_from - 1.0 and arr[-1] <= (a_to - a_from) + 1.0:   # توقيتات نسبية لبداية الآية
        arr = [x + a_from for x in arr]
    in_range = all(a_from - 0.6 <= x <= a_to + 0.6 for x in arr)
    increasing = all(arr[i] <= arr[i + 1] for i in range(len(arr) - 1))
    return arr if (in_range and increasing) else None


def chunk_times(ayah_ts, words, plan):
    a_from, a_to = ayah_ts["from"], ayah_ts["to"]
    starts = word_starts(ayah_ts, len(words))
    if starts:
        bounds = [starts[i0] for i0, _ in plan]
        bounds[0] = a_from
        ends = bounds[1:] + [a_to]
        if all(e - s >= 0.2 for s, e in zip(bounds, ends)):
            return list(zip(bounds, ends))
    # بديل: توزيع حسب عدد الحروف
    weights = [max(1, sum(len(w) for w in words[i0:i1])) for i0, i1 in plan]
    total, t, out = sum(weights), a_from, []
    for wt in weights:
        dt = (a_to - a_from) * wt / total
        out.append((t, t + dt))
        t += dt
    return out


# ----------------------------------------------------------------------------
# 6. رسم الشرائح
# ----------------------------------------------------------------------------
def load_font(path, size):
    return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)


def fit_font_size(font_path, texts, start=100, minimum=52, max_width=W - 160):
    """حجم خط واحد ثابت لكل الفيديو (يمنع تغير الحجم بين الشرائح)."""
    size = start
    while size > minimum:
        font = load_font(font_path, size)
        if all(font.getlength(t, direction="rtl", language="ar") <= max_width for t in texts):
            break
        size -= 4
    return size


def render_slide(font_path, font_size, phrase, header, out_path):
    img = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cx, y = W // 2, int(H * 0.46)

    header_font = load_font(font_path, 50)
    font = load_font(font_path, font_size)

    # ظل ناعم (Blur) خلف النصوص لرفع التباين
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(shadow)
    sd.text((cx, 235), header, font=header_font, fill=(0, 0, 0, 200), anchor="mm", direction="rtl", language="ar")
    sd.text((cx, y + 6), phrase, font=font, fill=(0, 0, 0, 230), anchor="mm", direction="rtl", language="ar")
    shadow = shadow.filter(ImageFilter.GaussianBlur(14))
    img = Image.alpha_composite(img, shadow)

    d = ImageDraw.Draw(img)
    d.text((cx, 235), header, font=header_font, fill=(255, 215, 90, 255), anchor="mm",
           direction="rtl", language="ar", stroke_width=3, stroke_fill=(0, 0, 0, 200))
    d.text((cx, y), phrase, font=font, fill=(255, 255, 255, 255), anchor="mm",
           direction="rtl", language="ar", stroke_width=6, stroke_fill=(0, 0, 0, 235))
    img.save(out_path, "PNG")


# ----------------------------------------------------------------------------
# 7. بناء الفيديو
# ----------------------------------------------------------------------------
def encode(cmd_builder, out_file):
    """يجرب أكثر من CRF حتى يدخل الحجم تحت حد تليجرام."""
    for crf in (19, 23, 27):
        run(cmd_builder(crf), f"ffmpeg (crf={crf})")
        size = os.path.getsize(out_file)
        print(f"[*] CRF {crf} → {size / 1024 / 1024:.1f} MB")
        if size <= TELEGRAM_LIMIT_BYTES:
            break
    return os.path.getsize(out_file)


def build_video(surah_num, reciter_key, reciter_name, surah_name, verses_text, timestamps,
                audio_url, start_ayah, end_ayah, bg_video):
    font_path = find_font()

    # --- الصوت ---
    full_audio = os.path.join(TEMP_DIR, f"full_{surah_num}_{reciter_key}.mp3")
    if not os.path.exists(full_audio):
        tmp = full_audio + ".part"
        with HTTP.get(audio_url, stream=True, timeout=180) as r:
            r.raise_for_status()
            with open(tmp, "wb") as f:
                for chunk in r.iter_content(chunk_size=1024 * 512):
                    f.write(chunk)
        os.replace(tmp, full_audio)

    t_start = timestamps[start_ayah]["from"]
    t_end = timestamps[end_ayah]["to"]

    # تأكد إن ملف الصوت فعلاً يغطي التوقيتات (يكشف عدم تطابق الملف مع بيانات التوقيت)
    full_dur = probe_duration(full_audio)
    if t_end > full_dur + 0.5:
        raise RuntimeError(f"التوقيت ({t_end:.1f}ث) أطول من ملف الصوت ({full_dur:.1f}ث): "
                           f"الملف مش مطابق لبيانات التوقيت، تم الإيقاف.")

    cut_audio = os.path.join(TEMP_DIR, "cut.mp3")
    # إعادة ترميز (بدل copy) + seek دقيق بعد -i لضمان مزامنة الكلمات
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", full_audio,
         "-ss", f"{t_start:.3f}", "-to", f"{t_end:.3f}", "-c:a", "libmp3lame", "-q:a", "2", cut_audio],
        "قص الصوت")
    duration = probe_duration(cut_audio)

    # --- الشرائح ---
    slides = []   # (نص, بداية, نهاية)
    for ayah in range(start_ayah, end_ayah + 1):
        words = split_words(verses_text[ayah])
        plan = plan_chunks(words)
        times = chunk_times(timestamps[ayah], words, plan)
        num_ar = str(ayah).translate(ARABIC_DIGITS)
        for k, ((i0, i1), (s, e)) in enumerate(zip(plan, times)):
            text = " ".join(words[i0:i1])
            if k == len(plan) - 1:
                text += f" \u06dd{num_ar}"
            s = max(0.0, s - t_start)
            e = min(duration, e - t_start)
            if e - s > 0.05:
                slides.append((text, s, e))

    if not slides:
        raise RuntimeError("لا توجد شرائح لتصديرها")

    header = f"سورة {surah_name} | {reciter_name}"
    size = fit_font_size(font_path, [t for t, _, _ in slides])
    print(f"[*] عدد الشرائح: {len(slides)} | حجم الخط: {size}")

    slide_files = []
    for i, (text, s, e) in enumerate(slides):
        p = os.path.join(TEMP_DIR, f"phrase_{i}.png")
        render_slide(font_path, size, text, header, p)
        slide_files.append((p, s, e))

    # --- ffmpeg ---
    inputs = []
    if bg_video and os.path.exists(bg_video):
        inputs += ["-stream_loop", "-1", "-i", bg_video]
        grade = ("scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,"
                 f"fps={FPS},"
                 "eq=saturation=1.25:contrast=1.1,"
                 "unsharp=5:5:0.6:5:5:0.0,"
                 "vignette=PI/5,"
                 "drawbox=x=0:y=0:w=iw:h=ih:t=fill:c=black@0.30,"
                 "format=yuv420p")
        filters = f"[0:v]{grade}[bg];"
        last = "[bg]"
    else:
        inputs += ["-f", "lavfi", "-i", f"color=c=0x08131d:s={W}x{H}:r={FPS}:d={duration:.3f}"]
        filters = "[0:v]format=yuv420p[bg];"
        last = "[bg]"

    for idx, (path, s, e) in enumerate(slide_files, start=1):
        d = e - s
        fade = max(0.05, min(0.2, d / 3))
        inputs += ["-loop", "1", "-framerate", str(FPS), "-t", f"{d:.3f}", "-i", path]
        filters += (f"[{idx}:v]format=rgba,"
                    f"fade=t=in:st=0:d={fade:.3f}:alpha=1,"
                    f"fade=t=out:st={d - fade:.3f}:d={fade:.3f}:alpha=1,"
                    f"setpts=PTS+{s:.3f}/TB[s{idx}];")
        filters += (f"{last}[s{idx}]overlay=0:0:eof_action=pass:"
                    f"enable='between(t,{s:.3f},{e:.3f})'[v{idx}];")
        last = f"[v{idx}]"
    audio_idx = len(slide_files) + 1
    filters = filters.rstrip(";")

    afilter = (f"loudnorm=I=-16:TP=-1.5:LRA=11,"
               f"afade=t=in:st=0:d=0.15,afade=t=out:st={max(0.0, duration - 0.5):.3f}:d=0.5")

    out_file = os.path.join(OUT_DIR, f"Surah_{surah_num}_{start_ayah}-{end_ayah}_{reciter_key}.mp4")

    def cmd_builder(crf):
        return ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                *inputs, "-i", cut_audio,
                "-filter_complex", filters,
                "-map", last, "-map", f"{audio_idx}:a",
                "-af", afilter,
                "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                "-maxrate", "6M", "-bufsize", "12M",
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
                "-movflags", "+faststart",
                "-t", f"{duration:.3f}", out_file]

    print("[*] جاري تصدير الفيديو...")
    size_bytes = encode(cmd_builder, out_file)
    if size_bytes > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"حجم الفيديو تجاوز 100 ميجا: {size_bytes / 1024 / 1024:.1f} MB")

    # تنظيف ملفات الشرائح المؤقتة
    for p, _, _ in slide_files:
        try:
            os.remove(p)
        except OSError:
            pass

    return out_file, duration


def generate_video(conn, surah_num, reciter_key, bg_video=None):
    reciter = RECITERS[reciter_key]
    print(f"[*] جلب بيانات سورة {surah_num} للقارئ {reciter['name']}...")
    surah_name, verses, timestamps, audio_url, rid = fetch_surah_data(surah_num, reciter_key)
    print(f"[*] القارئ id={rid} | مصدر الصوت: {audio_url}")

    clip = find_next_clip(conn, surah_num, timestamps, reciter_key)
    if not clip:
        return None
    start_ayah, end_ayah, est = clip
    # بنتحقق من توقيت الآيات المستخدمة في المقطع ده بس (غلط في آية بعيدة ما يوقفش المقطع)
    validate_timestamps({a: timestamps[a] for a in range(start_ayah, end_ayah + 1)})
    print(f"[+] سورة {surah_name}: الآيات {start_ayah}-{end_ayah} (≈{est:.1f} ث)")

    if bg_video is None:
        bg_video = get_pixabay_video()

    out_file, duration = build_video(surah_num, reciter_key, reciter["name"], surah_name, verses,
                                     timestamps, audio_url, start_ayah, end_ayah, bg_video)
    title = (f"سورة {surah_name} (الآيات {start_ayah}-{end_ayah}) | "
             f"القارئ {reciter['name']} #قرآن #shorts")
    return {"path": out_file, "title": title, "surah": surah_num, "start": start_ayah,
            "end": end_ayah, "duration": duration}


# ----------------------------------------------------------------------------
# 8. التسليم: Telegram / GitHub / Make
# ----------------------------------------------------------------------------
def send_to_telegram(clip):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[!] بيانات Telegram غير مكتملة، تم التخطي.")
        return False
    size = os.path.getsize(clip["path"])
    if size > 50 * 1024 * 1024:
        print("[!] الفيديو أكبر من 50MB (حد تليجرام للبوتات)، تم التخطي.")
        return False
    try:
        with open(clip["path"], "rb") as f:
            res = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendVideo",
                data={"chat_id": TELEGRAM_CHAT_ID, "caption": clip["title"][:1024],
                      "supports_streaming": "true", "width": W, "height": H,
                      "duration": int(clip["duration"])},
                files={"video": f}, timeout=600)
        if res.status_code == 200:
            print("[✓] تم الإرسال إلى تليجرام")
            return True
        print(f"[!] فشل تليجرام: {res.status_code} - {res.text[:300]}")
    except Exception as e:
        print(f"[!] خطأ تليجرام: {e}")
    return False


def upload_to_github_release(video_path, tag_name="latest", keep_assets=15):
    if not GITHUB_TOKEN or not GITHUB_REPOSITORY:
        print("[!] بيانات GitHub غير متوفرة.")
        return None

    file_name = os.path.basename(video_path)
    headers = {"Authorization": f"Bearer {GITHUB_TOKEN}", "Accept": "application/vnd.github+json"}
    api = f"https://api.github.com/repos/{GITHUB_REPOSITORY}"

    res = requests.get(f"{api}/releases/tags/{tag_name}", headers=headers, timeout=60)
    if res.status_code == 200:
        release = res.json()
    else:
        res = requests.post(f"{api}/releases", headers=headers, timeout=60,
                            json={"tag_name": tag_name, "name": "Latest Quran Clip",
                                  "draft": False, "prerelease": False})
        res.raise_for_status()
        release = res.json()
    release_id = release["id"]

    assets = release.get("assets", [])
    for a in assets:                                   # احذف نسخة بنفس الاسم
        if a["name"] == file_name:
            requests.delete(a["url"], headers=headers, timeout=60)
    old = sorted([a for a in assets if a["name"] != file_name], key=lambda a: a["created_at"])
    for a in old[:-keep_assets] if keep_assets and len(old) > keep_assets else []:
        requests.delete(a["url"], headers=headers, timeout=60)

    upload_url = f"https://uploads.github.com/repos/{GITHUB_REPOSITORY}/releases/{release_id}/assets"
    with open(video_path, "rb") as f:
        up = requests.post(upload_url, params={"name": file_name},
                           headers={**headers, "Content-Type": "video/mp4"}, data=f, timeout=900)
    up.raise_for_status()
    url = up.json()["browser_download_url"]
    print(f"[✓] تم الرفع إلى GitHub: {url}")
    return url


def notify_make(video_url, video_title):
    if not MAKE_WEBHOOK_URL:
        print("[!] MAKE_WEBHOOK_URL غير موجود.")
        return False
    try:
        res = requests.post(MAKE_WEBHOOK_URL, json={"video_url": video_url, "title": video_title}, timeout=60)
        if res.status_code in (200, 204):
            print("[✓] استلم Make.com الطلب")
            return True
        print(f"[!] خطأ Make: {res.status_code} - {res.text[:300]}")
    except Exception as e:
        print(f"[!] خطأ Make: {e}")
    return False


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------
def main():
    if os.environ.get("CHECK_RECITERS") == "1":
        return check_reciters()

    check_environment()
    if PROGRESS_MODE not in ("shared", "per_reciter"):
        raise SystemExit(f"PROGRESS_MODE غير معروف: {PROGRESS_MODE} (المتاح: shared / per_reciter)")
    reciter_key = os.environ.get("RECITER", "minshawi")
    if reciter_key not in RECITERS:
        raise SystemExit(f"RECITER غير معروف: {reciter_key}. المتاح: {', '.join(RECITERS)}")

    conn = init_db()
    fixed = (os.environ.get("SURAH") or "").strip()
    if fixed:
        surahs = [int(fixed)]
        print(f"[i] سورة ثابتة: {fixed}")
    else:
        first = resume_surah(conn, reciter_key, int(os.environ.get("START_SURAH", "2")))
        surahs = list(range(first, 115))
        print(f"[i] وضع التقدم: {PROGRESS_MODE} | هنكمل من سورة {first}")

    clip = None
    for s in surahs:
        clip = generate_video(conn, s, reciter_key)
        if clip:
            break
        print(f"[i] سورة {s} خلصت لهذا القارئ، ننتقل للتالية...")
    if not clip:
        print("[!] لا توجد مقاطع جديدة.")
        return 0

    tg_ok = send_to_telegram(clip)
    gh_url = None
    try:
        gh_url = upload_to_github_release(clip["path"])
    except Exception as e:
        print(f"[!] فشل رفع GitHub: {e}")
    make_ok = notify_make(gh_url, clip["title"]) if gh_url else False

    delivered = make_ok if MAKE_WEBHOOK_URL else (tg_ok or bool(gh_url))
    if delivered:
        mark_clip_done(conn, clip["surah"], clip["start"], clip["end"], reciter_key, clip["duration"])
        print("[✓] تم تسجيل المقطع كمنتهٍ.")
        return 0
    print("[!] لم يكتمل التسليم، لن يتم تسجيل المقطع (سيُعاد المحاولة في التشغيل القادم).")
    return 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
