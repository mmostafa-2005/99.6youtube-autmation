#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Quran short-video generator (v2.2: ظهور كلمة بكلمة + شكل مشغّل صوت)

المتطلبات على GitHub Actions:
    sudo apt-get install -y ffmpeg fonts-hosny-amiri libraqm0 libfribidi0 libharfbuzz0b
    pip install requests pillow

متغيرات بيئة اختيارية:
    RECITER         مفتاح القارئ من القاموس RECITERS (الافتراضي: minshawi)
    SURAH           رقم سورة ثابت (لو مش محدد بيكمل تلقائياً من آخر مقطع ويتنقل للسورة التالية)
    START_SURAH     أول سورة تبدأ بها لو قاعدة البيانات فاضية (الافتراضي: 2)
    FONT_PATH       مسار خط بديل
    PROGRESS_MODE   shared (الافتراضي) | per_reciter
    CHECK_RECITERS  لو = 1: يفحص كل القراء المعرّفين ويطبع النتيجة فقط
    BG_MODE         black (الافتراضي: خلفية سوداء زي المشغّل) | video (فيديو طبيعة من Pixabay)
    LINES_PER_CARD  عدد الأسطر في الكارت: 1 أو 2 (الافتراضي 2) - كل سطر 4 كلمات بالكتير
    HIGHLIGHT       لو = 1: الكلمة اللي بتتقال دلوقتي تظهر بلون ذهبي (الافتراضي 0)
    FADE_FRAMES     مدة fade-in لكل كلمة بالفريمات (الافتراضي 6 ≈ 0.2 ثانية، 1 = من غير fade)
    LOGO_PATH       مسار اللوجو (الافتراضي: logo.png / logo.jpg / logo.jpeg جنب السكريبت)
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
from PIL import Image, ImageDraw, ImageFont, features
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# ----------------------------------------------------------------------------
# الإعدادات
# ----------------------------------------------------------------------------
_ABDULBASIT = ("abdulbaset", "abdulbasit", "abdulbasid", "abdulsamad")
RECITERS = {
    "minshawi": {"ids": [9], "name": "محمد صديق المنشاوي",
                 "expect": [("minshawi", "menshawi", "minshawy"), ("murattal",)]},
    "dussary": {"ids": [97], "name": "ياسر الدوسري",
                "expect": [("dussary", "dosari", "dosary", "dossari")]},
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
HEADERS = {"User-Agent": "quran-video-generator/2.2"}
ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")

W, H = 1080, 1920
FPS = 30
MIN_CLIP_SEC, MAX_CLIP_SEC = 25.0, 65.0
MAX_WORDS_PER_LINE = 4

PROGRESS_MODE = os.environ.get("PROGRESS_MODE", "shared").strip().lower()
BG_MODE = os.environ.get("BG_MODE", "black").strip().lower()
LINES_PER_CARD = min(2, max(1, int(os.environ.get("LINES_PER_CARD", "2") or 2)))
HIGHLIGHT = os.environ.get("HIGHLIGHT", "0").strip() == "1"
FADE_FRAMES = max(1, int(os.environ.get("FADE_FRAMES", "6") or 6))   # مدة الـ fade-in بالفريمات (6 ≈ 0.2 ث)
LOGO_PATH = os.environ.get("LOGO_PATH", "").strip()

MAX_FILE_SIZE_BYTES = 100 * 1024 * 1024
TELEGRAM_LIMIT_BYTES = 48 * 1024 * 1024

MAKE_WEBHOOK_URL = os.environ.get("MAKE_WEBHOOK_URL")
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
GITHUB_REPOSITORY = os.environ.get("GITHUB_REPOSITORY")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")

STOP_MARKS = "\u06d6\u06d7\u06d8\u06d9\u06da\u06db\u06dc"
STANDALONE_MARKS = set(STOP_MARKS) | {"\u06de", "\u06e9"}
PHRASE_BREAK_MARKS = set("\u06d7\u06d8\u06da\u06db")

# ألوان واجهة المشغّل
WHITE = (255, 255, 255, 255)
GOLD = (255, 215, 90, 255)
GREY = (150, 150, 150, 255)
RING = (85, 85, 85, 255)
BTN_FILL = (22, 22, 22, 255)
BTN_LINE = (55, 55, 55, 255)
BAR_TRACK = (38, 38, 38, 255)
BAR_FILL = (232, 228, 218, 255)


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


def find_ui_font():
    """خط واجهة المشغّل (Amiri Bold لو موجود، وإلا خط المصحف)."""
    for name in ("Amiri-Bold.ttf", "Amiri-Regular.ttf"):
        hits = glob.glob(f"/usr/share/fonts/**/{name}", recursive=True)
        if hits:
            return hits[0]
    return find_font()


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
# 2. جلب النصوص والتوقيتات (بدون تغيير)
# ----------------------------------------------------------------------------
def _norm(s):
    return "".join(ch for ch in str(s).lower() if ch.isalnum())


def url_matches(url, cfg):
    u = _norm(url)
    if any(_norm(x) in u for x in cfg.get("reject", ())):
        return False
    return all(any(_norm(alt) in u for alt in group) for group in cfg["expect"])


def resolve_chapter_audio(reciter_key, surah_num):
    cfg = RECITERS[reciter_key]
    candidates = [_RESOLVED[reciter_key]] if reciter_key in _RESOLVED else cfg["ids"]
    seen = []
    for rid in candidates:
        r = HTTP.get(f"{QCOM}/chapter_recitations/{rid}/{surah_num}",
                     params={"segments": "true"}, timeout=60)
        if r.status_code in (400, 404, 422):
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
# 3. اختيار المقطع التالي (بدون تغيير)
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
                return start, end, dur
            return start, end - 1, timestamps[end - 1]["to"] - timestamps[start]["from"]
        end += 1

    return start, total, timestamps[total]["to"] - timestamps[start]["from"]


# ----------------------------------------------------------------------------
# 4. فيديو الخلفية (بيشتغل بس لو BG_MODE=video)
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
# 5. تقسيم النص وتوقيت الكلمات
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


def plan_chunks(words, max_w=MAX_WORDS_PER_LINE):
    """يرجع قائمة (بداية, نهاية) لكل سطر: يقطع عند علامات الوقف ثم يوزع الكلمات بالتساوي (≤4)."""
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
    for p in phrases:
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

    if arr[0] < a_from - 1.0 and arr[-1] <= (a_to - a_from) + 1.0:
        arr = [x + a_from for x in arr]
    in_range = all(a_from - 0.6 <= x <= a_to + 0.6 for x in arr)
    increasing = all(arr[i] <= arr[i + 1] for i in range(len(arr) - 1))
    return arr if (in_range and increasing) else None


def word_start_times(ayah_ts, words):
    """وقت بداية كل كلمة (مطلق داخل ملف السورة). بديل: توزيع حسب عدد الحروف."""
    starts = word_starts(ayah_ts, len(words))
    if starts:
        return starts
    a_from, a_to = ayah_ts["from"], ayah_ts["to"]
    weights = [max(1, len(w)) for w in words]
    total, acc, out = sum(weights), 0, []
    for wt in weights:
        out.append(a_from + (a_to - a_from) * acc / total)
        acc += wt
    return out


# ----------------------------------------------------------------------------
# 6. واجهة المشغّل (شكل الصورة) + رسم الكلمات كلمة بكلمة
# ----------------------------------------------------------------------------
def load_font(path, size):
    return ImageFont.truetype(path, size, layout_engine=ImageFont.Layout.RAQM)


def ar_len(font, text):
    return font.getlength(text, direction="rtl", language="ar")


def fit_font_size(font_path, texts, start=96, minimum=44, max_width=W - 230):
    """حجم خط واحد ثابت لكل الفيديو (يمنع تغير الحجم بين الأسطر)."""
    size = start
    while size > minimum:
        font = load_font(font_path, size)
        if all(ar_len(font, t) <= max_width for t in texts):
            break
        size -= 4
    return size


def fit_ui(path, text, size, max_w, minimum=30):
    while size > minimum:
        f = load_font(path, size)
        if ar_len(f, text) <= max_w:
            return f
        size -= 2
    return load_font(path, minimum)


def fmt_time(sec):
    sec = int(max(0, sec))
    return f"{sec // 60:02d}:{sec % 60:02d}"


def ar_text(d, xy, text, font, fill, anchor="rm"):
    d.text(xy, text, font=font, fill=fill, anchor=anchor, direction="rtl", language="ar")


def draw_chip(d, right, cy, label, value, font, h=84):
    pad, gap = 28, 10
    lw, vw = ar_len(font, label), ar_len(font, value)
    left = right - (lw + gap + vw + 2 * pad)
    d.rounded_rectangle((left, cy - h // 2, right, cy + h // 2), radius=30,
                        fill=(16, 16, 16, 235), outline=RING, width=3)
    ar_text(d, (right - pad, cy), label, font, GREY)
    ar_text(d, (right - pad - lw - gap, cy), value, font, WHITE)
    return left


def circle_button(d, cx, cy, r=80):
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=BTN_FILL, outline=BTN_LINE, width=2)


def load_logo(diameter):
    """يحمّل اللوجو، يقصّه حوالين المحتوى، ويرجّعه دايرة بقطر diameter (أو None لو مش موجود)."""
    here = os.path.dirname(os.path.abspath(__file__))
    cands = [LOGO_PATH] if LOGO_PATH else []
    for n in ("logo.png", "logo.jpg", "logo.jpeg"):
        cands += [os.path.join(here, n), n]
    cands += sorted(glob.glob(os.path.join(here, "logo.*")) + glob.glob("logo.*"))   # أي امتداد/كتابة تانية (مثلاً logo.jepg)
    path = next((c for c in cands if c and os.path.exists(c)), None)
    if not path:
        print("[!] اللوجو مش موجود (logo.png)، هيتم استخدام الأسطوانة.")
        return None
    try:
        im = Image.open(path).convert("RGB")
        box = im.convert("L").point(lambda p: 255 if p > 60 else 0).getbbox() or (0, 0, im.width, im.height)
        cx, cy = (box[0] + box[2]) / 2, (box[1] + box[3]) / 2
        side = max(box[2] - box[0], box[3] - box[1]) * 1.5
        im = im.crop((int(cx - side / 2), int(cy - side / 2), int(cx + side / 2), int(cy + side / 2)))
        im = im.resize((diameter, diameter), Image.LANCZOS).convert("RGBA")
        mask = Image.new("L", (diameter * 4, diameter * 4), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, diameter * 4 - 1, diameter * 4 - 1), fill=255)
        im.putalpha(mask.resize((diameter, diameter), Image.LANCZOS))
        return im
    except Exception as e:
        print(f"[!] تعذر تحميل اللوجو: {e}")
        return None


def make_renderer(font_path, ui_path, size, pages, header_text, reciter_name, source_text,
                  full_dur, t_start):
    """يبني الطبقة الثابتة مرة واحدة ويرجّع دالة render(state, sec) -> bytes (RGBA خام)."""
    qfont = load_font(font_path, size)
    line_h = int(size * 1.55)
    card_h = LINES_PER_CARD * line_h + 120

    OY = 150                                  # إزاحة لتحت (بعيداً عن واجهة شورتس العلوية)
    card_top = OY + 470
    cy = card_top + card_h // 2
    bar_y = card_top + card_h + 90
    times_y = bar_y + 64
    ctrl_y = bar_y + 250
    BX0, BX1 = 60, W - 60

    base = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(base)

    # مقبض السحب
    d.rounded_rectangle((W // 2 - 55, OY + 50, W // 2 + 55, OY + 62), radius=6, fill=(105, 105, 105, 255))

    # زر السهم لأسفل
    bx, by = 110, OY + 190
    circle_button(d, bx, by, 62)
    d.line([(bx - 20, by - 8), (bx, by + 12), (bx + 20, by - 8)], fill=WHITE, width=8, joint="curve")

    # الأسطوانة
    vx, vy = W - 150, OY + 205
    d.ellipse((vx - 95, vy - 95, vx + 95, vy + 95), fill=(10, 10, 10, 255), outline=(95, 95, 95, 255), width=6)
    d.ellipse((vx - 82, vy - 82, vx + 82, vy + 82), outline=(45, 45, 45, 255), width=2)
    logo = load_logo(2 * 86)
    if logo is not None:
        base.alpha_composite(logo, (vx - 86, vy - 86))
        d = ImageDraw.Draw(base)
        d.ellipse((vx - 88, vy - 88, vx + 88, vy + 88), outline=(235, 235, 235, 255), width=3)
    else:
        d.ellipse((vx - 66, vy - 66, vx + 66, vy + 66), outline=WHITE, width=9)
        d.ellipse((vx - 13, vy - 13, vx + 13, vy + 13), fill=(150, 150, 150, 255))

    # النصوص العلوية (السورة + نطاق الآيات / القارئ)
    xr = W - 290
    ar_text(d, (xr, OY + 160), header_text, fit_ui(ui_path, header_text, 46, xr - 190), WHITE)
    ar_text(d, (xr, OY + 250), reciter_name, fit_ui(ui_path, reciter_name, 62, xr - 190), WHITE)

    # الشارتان (المصدر / النسخة)
    chips = [("المصدر:", source_text), ("النسخة:", "نسخة أصلية")]
    chip_size = 36
    while chip_size > 24:
        cf = load_font(ui_path, chip_size)
        total = sum(ar_len(cf, a) + 10 + ar_len(cf, b) + 56 for a, b in chips) + 20 * (len(chips) - 1)
        if total <= W - 60 - 190:
            break
        chip_size -= 2
    cf = load_font(ui_path, chip_size)
    right = W - 60
    for a, b in chips:
        right = draw_chip(d, right, OY + 385, a, b, cf) - 20

    # زر العين
    ex, ey = 110, OY + 385
    circle_button(d, ex, ey, 50)
    d.ellipse((ex - 28, ey - 16, ex + 28, ey + 16), outline=WHITE, width=4)
    d.ellipse((ex - 8, ey - 8, ex + 8, ey + 8), outline=WHITE, width=4)

    # كارت الآية
    d.rounded_rectangle((50, card_top, W - 50, card_top + card_h), radius=70,
                        fill=(0, 0, 0, 215), outline=(82, 82, 82, 255), width=3)

    # شريط التقدم (المسار)
    d.rounded_rectangle((BX0, bar_y - 8, BX1, bar_y + 8), radius=8, fill=BAR_TRACK)

    # أزرار التحكم: تقديم | إيقاف | رجوع | تكرار
    xs = [W // 2 - 300, W // 2 - 100, W // 2 + 100, W // 2 + 300]
    circle_button(d, xs[0], ctrl_y)
    for dx in (-14, 10):
        x = xs[0] + dx
        d.polygon([(x - 12, ctrl_y - 18), (x - 12, ctrl_y + 18), (x + 16, ctrl_y)], fill=WHITE)
    d.rounded_rectangle((xs[1] - 38, ctrl_y - 62, xs[1] - 10, ctrl_y + 62), radius=8, fill=WHITE)
    d.rounded_rectangle((xs[1] + 10, ctrl_y - 62, xs[1] + 38, ctrl_y + 62), radius=8, fill=WHITE)
    circle_button(d, xs[2], ctrl_y)
    for dx in (-10, 14):
        x = xs[2] + dx
        d.polygon([(x + 12, ctrl_y - 18), (x + 12, ctrl_y + 18), (x - 16, ctrl_y)], fill=WHITE)
    circle_button(d, xs[3], ctrl_y)
    x = xs[3]
    d.line([(x - 28, ctrl_y - 16), (x + 18, ctrl_y - 16)], fill=WHITE, width=6)
    d.polygon([(x + 16, ctrl_y - 30), (x + 16, ctrl_y - 2), (x + 36, ctrl_y - 16)], fill=WHITE)
    d.line([(x + 28, ctrl_y + 16), (x - 18, ctrl_y + 16)], fill=WHITE, width=6)
    d.polygon([(x - 16, ctrl_y + 2), (x - 16, ctrl_y + 30), (x - 36, ctrl_y + 16)], fill=WHITE)

    # تخطيط كلمات كل صفحة (يمين → يسار، كل كلمة بمكان ثابت)
    sp = max(qfont.getlength(" ", direction="rtl", language="ar"), size * 0.25)
    layouts = []
    for pg in pages:
        n = len(pg["lines"])
        lay = []
        for li, line in enumerate(pg["lines"]):
            widths = [ar_len(qfont, u) for u in line]
            total = sum(widths) + sp * (len(widths) - 1)
            x = W / 2 + total / 2
            yc = cy + (li - (n - 1) / 2) * line_h
            pos = []
            for w in widths:
                pos.append((x, yc + size * 0.28, w))
                x -= w + sp
            lay.append(pos)
        layouts.append([xy for line in lay for xy in line])
    flats = [[u for line in pg["lines"] for u in line] for pg in pages]

    f_time = load_font(ui_path, 42)
    total_s = int(round(full_dur))

    def render(state, sec):
        img = base.copy()
        dr = ImageDraw.Draw(img)
        pi, k, lvl = state
        for i, (u, (x, y, w)) in enumerate(list(zip(flats[pi], layouts[pi]))[:k]):
            color = GOLD if (HIGHLIGHT and i == k - 1) else WHITE
            if i == k - 1 and lvl < FADE_FRAMES:          # آخر كلمة: fade-in
                x0, y0 = int(x - w - 30), int(y - size * 1.0)
                x1, y1 = int(x + 30), int(y + size * 0.6)
                layer = Image.new("RGBA", (x1 - x0, y1 - y0), color[:3] + (0,))
                ImageDraw.Draw(layer).text((x - x0, y - y0), u, font=qfont,
                                           fill=color[:3] + (int(255 * lvl / FADE_FRAMES),),
                                           anchor="rs", direction="rtl", language="ar")
                img.alpha_composite(layer, (x0, y0))
                dr = ImageDraw.Draw(img)
            else:
                dr.text((x, y), u, font=qfont, fill=color, anchor="rs", direction="rtl", language="ar")
        frac = min(1.0, sec / total_s) if total_s > 0 else 0.0
        fw = int((BX1 - BX0) * frac)
        if fw > 0:
            dr.rounded_rectangle((BX0, bar_y - 8, BX0 + max(fw, 16), bar_y + 8), radius=8, fill=BAR_FILL)
        dr.text((BX0, times_y), fmt_time(sec), font=f_time, fill=GREY, anchor="lm", direction="ltr")
        dr.text((BX1, times_y), "-" + fmt_time(total_s - sec), font=f_time, fill=GREY,
                anchor="rm", direction="ltr")
        return img.tobytes()

    return render


def build_timeline(pages, duration, t_start):
    """يرجّع قائمة مقاطع (بداية, نهاية, (صفحة, عدد الكلمات الظاهرة), ثانية_المشغّل)."""
    events = []
    for pi, pg in enumerate(pages):
        flat = [t for line in pg["starts"] for t in line]
        for k, t in enumerate(flat, start=1):
            events.append((min(max(t, 0.0), duration), pi, k))
    events.sort(key=lambda e: e[0])
    states = [(0.0, (0, 0))] + [(t, (pi, k)) for t, pi, k in events]
    fade_pts = {t + i / FPS for t, _, _ in events for i in range(1, FADE_FRAMES)}

    ticks = [s - t_start for s in range(int(t_start) + 1, int(t_start + duration) + 1)]
    bounds = sorted({0.0, duration} | {t for t, _ in states} | {x for x in ticks if 0 < x < duration} | {x for x in fade_pts if 0 < x < duration})
    segs, j, cur, cur_t = [], 0, states[0][1], 0.0
    for a, b in zip(bounds, bounds[1:]):
        while j < len(states) and states[j][0] <= a + 1e-6:
            cur_t, cur = states[j]
            j += 1
        if b - a > 1e-6:
            lvl = FADE_FRAMES if cur[1] == 0 else min(FADE_FRAMES, int((a - cur_t) * FPS + 1e-6) + 1)
            segs.append((a, b, (cur[0], cur[1], lvl), int(t_start + a + 1e-6)))
    return segs


# ----------------------------------------------------------------------------
# 7. بناء الفيديو
# ----------------------------------------------------------------------------
def stream_encode(cmd, frames):
    """يبعت الفريمات الخام لـ ffmpeg عن طريق pipe."""
    p = subprocess.Popen(cmd, stdin=subprocess.PIPE, stderr=subprocess.PIPE)
    try:
        for buf, n in frames:
            for _ in range(n):
                p.stdin.write(buf)
        p.stdin.close()
    except BrokenPipeError:
        pass
    err = p.stderr.read().decode(errors="replace")
    if p.wait() != 0:
        raise RuntimeError(f"فشل ffmpeg:\n{err[-3000:]}")


def encode(cmd_builder, frames_factory, out_file):
    """يجرب أكثر من CRF حتى يدخل الحجم تحت حد تليجرام."""
    for crf in (19, 23, 27):
        stream_encode(cmd_builder(crf), frames_factory())
        size = os.path.getsize(out_file)
        print(f"[*] CRF {crf} → {size / 1024 / 1024:.1f} MB")
        if size <= TELEGRAM_LIMIT_BYTES:
            break
    return os.path.getsize(out_file)


def build_video(surah_num, reciter_key, reciter_name, surah_name, verses_text, timestamps,
                audio_url, start_ayah, end_ayah, bg_video):
    font_path = find_font()
    ui_path = find_ui_font()

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

    full_dur = probe_duration(full_audio)
    if t_end > full_dur + 0.5:
        raise RuntimeError(f"التوقيت ({t_end:.1f}ث) أطول من ملف الصوت ({full_dur:.1f}ث): "
                           f"الملف مش مطابق لبيانات التوقيت، تم الإيقاف.")

    cut_audio = os.path.join(TEMP_DIR, "cut.mp3")
    run(["ffmpeg", "-y", "-hide_banner", "-loglevel", "error", "-i", full_audio,
         "-ss", f"{t_start:.3f}", "-to", f"{t_end:.3f}", "-c:a", "libmp3lame", "-q:a", "2", cut_audio],
        "قص الصوت")
    duration = probe_duration(cut_audio)

    # --- الصفحات: كل صفحة = سطر أو سطرين (≤4 كلمات للسطر) من نفس الآية ---
    pages = []
    for ayah in range(start_ayah, end_ayah + 1):
        words = split_words(verses_text[ayah])
        if not words:
            continue
        plan = plan_chunks(words)
        starts = word_start_times(timestamps[ayah], words)
        num_ar = str(ayah).translate(ARABIC_DIGITS)
        units = words[:-1] + [f"{words[-1]} \u06dd{num_ar}"]      # رقم الآية يظهر مع آخر كلمة
        lines = [(units[a:b], [max(0.0, min(duration, s - t_start)) for s in starts[a:b]])
                 for a, b in plan]
        for i in range(0, len(lines), LINES_PER_CARD):
            grp = lines[i:i + LINES_PER_CARD]
            pages.append({"lines": [g[0] for g in grp], "starts": [g[1] for g in grp]})

    if not pages:
        raise RuntimeError("لا توجد صفحات لتصديرها")

    size = fit_font_size(font_path, [" ".join(l) for p in pages for l in p["lines"]])
    n_words = sum(len(l) for p in pages for l in p["lines"])
    print(f"[*] الصفحات: {len(pages)} | الكلمات: {n_words} | حجم الخط: {size}")

    # --- معلومات الواجهة (بتتغير مع كل مقطع) ---
    num_txt = str(surah_num).zfill(2).translate(ARABIC_DIGITS)
    a1 = str(start_ayah).translate(ARABIC_DIGITS)
    a2 = str(end_ayah).translate(ARABIC_DIGITS)
    rng = a1 if start_ayah == end_ayah else f"{a1}-{a2}"
    header_text = f"{num_txt}-{surah_name} {rng}"
    source_text = "المصحف المجوّد" if "mujawwad" in reciter_key else "المصحف المرتل"

    render = make_renderer(font_path, ui_path, size, pages, header_text, reciter_name, source_text,
                           full_dur, t_start)
    segs = build_timeline(pages, duration, t_start)

    def frames_factory():
        for a, b, state, sec in segs:
            n = round(b * FPS) - round(a * FPS)
            if n > 0:
                yield render(state, sec), n

    # --- ffmpeg: [0] خلفية | [1] فريمات الواجهة (pipe) | [2] الصوت ---
    if BG_MODE == "video" and bg_video and os.path.exists(bg_video):
        bg_inputs = ["-stream_loop", "-1", "-i", bg_video]
        bg_filter = ("[0:v]scale=1080:1920:force_original_aspect_ratio=increase:flags=lanczos,crop=1080:1920,"
                     f"fps={FPS},eq=saturation=1.25:contrast=1.1,unsharp=5:5:0.6:5:5:0.0,vignette=PI/5,"
                     "drawbox=x=0:y=0:w=iw:h=ih:t=fill:c=black@0.40,format=yuv420p[bg];")
    else:
        bg_inputs = ["-f", "lavfi", "-i", f"color=c=black:s={W}x{H}:r={FPS}:d={duration + 1:.3f}"]
        bg_filter = "[0:v]format=yuv420p[bg];"

    filters = bg_filter + "[bg][1:v]overlay=0:0:eof_action=repeat:format=auto,format=yuv420p[v]"
    afilter = (f"loudnorm=I=-16:TP=-1.5:LRA=11,"
               f"afade=t=in:st=0:d=0.15,afade=t=out:st={max(0.0, duration - 0.5):.3f}:d=0.5")

    out_file = os.path.join(OUT_DIR, f"Surah_{surah_num}_{start_ayah}-{end_ayah}_{reciter_key}.mp4")

    def cmd_builder(crf):
        return ["ffmpeg", "-y", "-hide_banner", "-loglevel", "error",
                *bg_inputs,
                "-f", "rawvideo", "-pix_fmt", "rgba", "-s", f"{W}x{H}", "-framerate", str(FPS), "-i", "-",
                "-i", cut_audio,
                "-filter_complex", filters,
                "-map", "[v]", "-map", "2:a",
                "-af", afilter,
                "-c:v", "libx264", "-preset", "medium", "-crf", str(crf),
                "-maxrate", "6M", "-bufsize", "12M",
                "-profile:v", "high", "-pix_fmt", "yuv420p", "-r", str(FPS),
                "-colorspace", "bt709", "-color_primaries", "bt709", "-color_trc", "bt709",
                "-c:a", "aac", "-b:a", "160k", "-ar", "44100",
                "-movflags", "+faststart",
                "-t", f"{duration:.3f}", out_file]

    print("[*] جاري تصدير الفيديو...")
    size_bytes = encode(cmd_builder, frames_factory, out_file)
    if size_bytes > MAX_FILE_SIZE_BYTES:
        raise ValueError(f"حجم الفيديو تجاوز 100 ميجا: {size_bytes / 1024 / 1024:.1f} MB")

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
    validate_timestamps({a: timestamps[a] for a in range(start_ayah, end_ayah + 1)})
    print(f"[+] سورة {surah_name}: الآيات {start_ayah}-{end_ayah} (≈{est:.1f} ث)")

    if BG_MODE == "video" and bg_video is None:
        bg_video = get_pixabay_video()

    out_file, duration = build_video(surah_num, reciter_key, reciter["name"], surah_name, verses,
                                     timestamps, audio_url, start_ayah, end_ayah, bg_video)
    title = (f"سورة {surah_name} (الآيات {start_ayah}-{end_ayah}) | "
             f"القارئ {reciter['name']} #قرآن #shorts")
    return {"path": out_file, "title": title, "surah": surah_num, "start": start_ayah,
            "end": end_ayah, "duration": duration}


# ----------------------------------------------------------------------------
# 8. التسليم: Telegram / GitHub / Make (بدون تغيير)
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
    for a in assets:
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
    if BG_MODE not in ("black", "video"):
        raise SystemExit(f"BG_MODE غير معروف: {BG_MODE} (المتاح: black / video)")
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
