import os
import json
import glob
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.http import MediaFileUpload

SERVICE_KEY_RAW = os.environ.get("GDRIVE_SERVICE_ACCOUNT_KEY")
FOLDER_ID = os.environ.get("GDRIVE_FOLDER_ID")

if not SERVICE_KEY_RAW or not FOLDER_ID:
    print("[!] بيانات Google Drive غير متوفرة في Secrets، تخطي الرفع.")
    exit(0)

def upload_to_drive():
    try:
        # قراءة المفتاح من المتغيرات
        key_dict = json.loads(SERVICE_KEY_RAW)
        creds = service_account.Credentials.from_service_account_info(
            key_dict,
            scopes=['https://www.googleapis.com/auth/drive']
        )
        service = build('drive', 'v3', credentials=creds)

        # البحث عن الفيديوهات الناتجة
        videos = glob.glob("generated_videos/*.mp4")
        if not videos:
            print("[!] لا توجد ملفات فيديو في generated_videos لرفعها.")
            return

        for video_path in videos:
            file_name = os.path.basename(video_path)
            print(f"[*] جاري رفع {file_name} إلى Google Drive...")

            file_metadata = {
                'name': file_name,
                'parents': [FOLDER_ID]
            }
            media = MediaFileUpload(video_path, mimetype='video/mp4', resumable=True)

            uploaded_file = service.files().create(
                body=file_metadata,
                media_body=media,
                fields='id, webViewLink'
            ).execute()

            print(f"[✓] تم رفع الفيديو بنجاح! الرابط: {uploaded_file.get('webViewLink')}")

    except Exception as e:
        print(f"[!] خطأ أثناء الرفع إلى Google Drive: {e}")

if __name__ == "__main__":
    upload_to_drive()
