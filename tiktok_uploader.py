import os
import requests

CLIENT_KEY = os.getenv("TIKTOK_CLIENT_KEY")
CLIENT_SECRET = os.getenv("TIKTOK_CLIENT_SECRET")
REFRESH_TOKEN = os.getenv("TIKTOK_REFRESH_TOKEN")


def get_fresh_access_token():
    """تجديد التوكن تلقائياً باستخدام Refresh Token"""
    url = "https://open.tiktokapis.com/v2/oauth/token/"
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    data = {
        "client_key": CLIENT_KEY,
        "client_secret": CLIENT_SECRET,
        "grant_type": "refresh_token",
        "refresh_token": REFRESH_TOKEN,
    }
    res = requests.post(url, headers=headers, data=data).json()
    if "data" in res and "access_token" in res["data"]:
        return res["data"]["access_token"]
    elif "access_token" in res:
        return res["access_token"]
    raise Exception(f"Failed to refresh access token: {res}")


def upload_tiktok_video(video_path, title):
    access_token = get_fresh_access_token()
    video_size = os.path.getsize(video_path)

    # 1. تهيئة جلسة الرفع (Publish as draft / post)
    init_url = "https://open.tiktokapis.com/v2/post/publish/video/init/"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {
        "post_info": {
            "title": title[:150],  # الحد الأقصى للنص
            "privacy_level": "SELF_ONLY",  # في وضع الـ Sandbox تنزل كمسودة خاصة بك
            "disable_duet": False,
            "disable_comment": False,
            "disable_stitch": False,
        },
        "source_info": {
            "source": "FILE_UPLOAD",
            "video_size": video_size,
            "chunk_size": video_size,
            "total_chunk_count": 1,
        },
    }

    init_res = requests.post(init_url, json=payload, headers=headers).json()
    if init_res.get("error", {}).get("code") != "ok":
        raise Exception(f"TikTok Init Error: {init_res}")

    upload_url = init_res["data"]["upload_url"]
    publish_id = init_res["data"]["publish_id"]

    # 2. رفع ملف الفيديو الفعلي
    upload_headers = {
        "Content-Range": f"bytes 0-{video_size - 1}/{video_size}",
        "Content-Type": "video/mp4",
    }

    with open(video_path, "rb") as video_file:
        upload_res = requests.put(
            upload_url, data=video_file, headers=upload_headers
        )

    if upload_res.status_code in [200, 201]:
        print(
            f"Successfully uploaded to TikTok! Publish ID: {publish_id}"
        )
    else:
        print(
            f"Upload failed: {upload_res.status_code} - {upload_res.text}"
        )


if __name__ == "__main__":
    # مسار الفيديو والنص المولد
    upload_tiktok_video("output.mp4", "تلاوة خاشعة #قرآن #quran")
