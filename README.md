# Marketing Video Pipeline (edge-tts → Dropbox)

Nhập kịch bản + link ảnh/video → GitHub Actions tự dựng video (giọng đọc Microsoft
miễn phí qua `edge-tts`) → tự động upload lên Dropbox. Không cần chạy gì ở máy local,
không cần UI Streamlit.

## Cách dùng

1. Push repo này lên GitHub.
2. Vào **Settings → Secrets and variables → Actions → New repository secret**, thêm 3 secret:
   - `DROPBOX_APP_KEY`
   - `DROPBOX_APP_SECRET`
   - `DROPBOX_REFRESH_TOKEN`
   (Lấy từ https://www.dropbox.com/developers/apps — dùng loại Refresh Token để không bao giờ hết hạn, giống app vd1 đang dùng.)
3. Vào tab **Actions → Marketing Video (edge-tts → Dropbox) → Run workflow**.
4. Điền các trường:
   - **media_urls** — link ảnh/video, cách nhau bằng dấu phẩy hoặc xuống dòng.
   - **script_text** — nội dung kịch bản, giọng đọc sẽ đọc đúng đoạn này.
   - **voice** — chọn giọng Microsoft từ dropdown (EN/VI).
   - **rate** — tốc độ đọc.
   - **resolution** — `tiktok` (dọc 1080×1920), `youtube` (ngang 1920×1080), `original` (giữ theo ảnh/video đầu tiên).
   - **motion** — `kenburns` (zoom nhẹ, chỉ áp dụng cho ảnh) hoặc `static`.
   - **output_name**, **dropbox_folder** — tuỳ chọn, có giá trị mặc định.
5. Bấm **Run workflow**. Xong sẽ thấy link Dropbox trong phần **Summary** của run.

## Cách hoạt động

- Nếu chỉ có ảnh: mỗi ảnh chiếm `độ dài giọng đọc / số ảnh`, có hiệu ứng zoom nhẹ (Ken Burns) tuỳ chọn.
- Nếu có video: video được cắt/loop cho khớp với phần thời lượng được chia, giữ hình gốc (không giữ audio gốc — audio cuối cùng luôn là giọng đọc).
- Có thể trộn cả ảnh và video trong cùng 1 danh sách `media_urls`, tool tự nhận diện theo đuôi file.
- Giọng đọc dùng `edge-tts` — thư viện gọi API Text-to-Speech miễn phí của Microsoft (không cần API key). Danh sách giọng đầy đủ: chạy `edge-tts --list-voices` hoặc xem https://github.com/rany2/edge-tts.

## Danh sách giọng có trong dropdown

| Giọng | Ngôn ngữ |
|---|---|
| en-US-AriaNeural | Tiếng Anh (Mỹ) - nữ |
| en-US-JennyNeural | Tiếng Anh (Mỹ) - nữ |
| en-US-GuyNeural | Tiếng Anh (Mỹ) - nam |
| en-US-AnaNeural | Tiếng Anh (Mỹ) - nữ, giọng trẻ em |
| en-GB-SoniaNeural | Tiếng Anh (Anh) - nữ |
| en-GB-RyanNeural | Tiếng Anh (Anh) - nam |
| en-AU-NatashaNeural | Tiếng Anh (Úc) - nữ |
| vi-VN-HoaiMyNeural | Tiếng Việt - nữ |
| vi-VN-NamMinhNeural | Tiếng Việt - nam |

Muốn thêm giọng khác chỉ cần thêm vào mục `options` của input `voice` trong
`.github/workflows/marketing-video.yml`.

## Giới hạn cần biết

- GitHub-hosted runner có timeout 30 phút/lần chạy (đủ cho video ngắn/vừa; video rất
  dài hoặc nhiều ảnh/video nặng nên cân nhắc runner tự host).
- `edge-tts` gọi ra máy chủ Microsoft qua mạng — nếu GitHub runner bị chặn mạng
  (hiếm khi xảy ra) bước tạo giọng sẽ báo lỗi rõ ràng trong log.
- Ảnh/video input phải là **link tải trực tiếp** (không phải link share Google
  Drive/Dropbox dạng xem trước) — có đuôi file rõ ràng (`.jpg`, `.mp4`,...).
