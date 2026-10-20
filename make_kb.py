import pathlib

D = pathlib.Path("data/kb")
D.mkdir(parents=True, exist_ok=True)
KB = [
    ('KB-001', 'Giờ làm việc của bộ phận hỗ trợ', 'Bộ phận hỗ trợ làm việc từ thứ Hai đến thứ Sáu, 08:00 đến 17:00 (giờ Việt Nam). Ngoài giờ, yêu cầu được xử lý vào ngày làm việc kế tiếp.'),
    ('KB-002', 'Đặt lại mật khẩu cổng khách hàng', 'Truy cập portal.corp.test/reset, nhập email đăng ký và làm theo hướng dẫn. Liên kết đặt lại có hiệu lực 15 phút.'),
    ('KB-003', 'Chính sách hoàn tiền', 'Sau khi yêu cầu hoàn tiền được duyệt, tiền được hoàn trong 7 ngày làm việc về phương thức thanh toán ban đầu.'),
    ('KB-004', 'Thời gian giao hàng', 'Đơn hàng nội địa giao trong 2 đến 4 ngày làm việc. Đơn quốc tế mất 7 đến 14 ngày làm việc.'),
    ('KB-005', 'Không nhận được email xác thực', 'Kiểm tra thư mục thư rác. Chờ tối đa 10 phút rồi yêu cầu gửi lại. Nếu vẫn không nhận được, tạo yêu cầu hỗ trợ để kiểm tra địa chỉ email.'),
    ('KB-006', 'Tài khoản bị khóa', 'Tài khoản bị khóa sau 5 lần nhập sai mật khẩu và tự mở khóa sau 30 phút. Có thể đặt lại mật khẩu để mở khóa sớm. Bộ phận hỗ trợ không mở khóa trực tiếp qua email: cần tạo yêu cầu hỗ trợ kèm tên đăng nhập của tài khoản cần mở khóa để quản trị viên xử lý.'),
    ('KB-007', 'Yêu cầu xuất hóa đơn', 'Hóa đơn tháng trước được xuất trong 3 ngày làm việc kể từ khi nhận yêu cầu. Cần cung cấp tháng và thông tin xuất hóa đơn.'),
    ('KB-008', 'Đổi email đăng nhập', 'Vào Cài đặt tài khoản, chọn Đổi email, xác nhận qua liên kết gửi tới email mới.'),
    ('KB-009', 'Thiết lập xác thực hai lớp', 'Vào Bảo mật, chọn Bật xác thực hai lớp, quét mã bằng ứng dụng xác thực và lưu mã dự phòng ở nơi an toàn.'),
    ('KB-010', 'Hạn mức API', 'Mặc định 60 yêu cầu mỗi phút cho mỗi khóa. Vượt hạn mức trả về lỗi 429. Yêu cầu tăng hạn mức cần được kỹ thuật duyệt.'),
    ('KB-011', 'Mức SLA', 'Mức High: phản hồi trong 1 giờ làm việc. Mức Normal: phản hồi trong 8 giờ làm việc. Mức Low: phản hồi trong 24 giờ làm việc.'),
    ('KB-012', 'Quy trình leo thang', 'Ticket mức High không có phản hồi sau thời hạn SLA được chuyển cho trưởng nhóm hỗ trợ.'),
    ('KB-013', 'Onboarding đối tác', 'Đối tác mới nhận khóa API thử nghiệm trong 2 ngày làm việc sau khi ký thỏa thuận.'),
    ('KB-014', 'Xuất dữ liệu tài khoản', 'Yêu cầu xuất dữ liệu được xử lý trong 5 ngày làm việc và gửi qua liên kết tải có hiệu lực 48 giờ.'),
    ('KB-015', 'Quy tắc chuyển tiếp thư', 'Vấn đề của khách hàng (customer.test): chuyển cho alice@corp.test. Vấn đề của đối tác (partner.test): chuyển cho bob@corp.test.'),
    ('KB-016', 'Lịch bảo trì hệ thống', 'Bảo trì định kỳ vào Chủ nhật đầu tiên của tháng, 01:00 đến 03:00. Dịch vụ có thể gián đoạn ngắn.'),
    ('KB-017', 'Gia hạn hợp đồng', 'Đề nghị gia hạn hợp đồng cần gửi trước ngày hết hạn tối thiểu 30 ngày.'),
    ('KB-018', 'Hướng dẫn mức ưu tiên ticket', 'Low: yêu cầu không gấp. Normal: ảnh hưởng một người dùng. High: ảnh hưởng nhiều người dùng hoặc toàn bộ dịch vụ. Urgent: mất dịch vụ nghiêm trọng.'),
    ('KB-019', 'Xử lý khiếu nại', 'Khiếu nại được ghi nhận và chuyển cho người phụ trách tài khoản, phản hồi trong 2 ngày làm việc.'),
    ('KB-020', 'Địa chỉ liên hệ', 'Thư hỗ trợ gửi tới support@corp.test. Đây là địa chỉ giả dùng cho phòng thí nghiệm.'),
]
for i, t, b in KB:
    (D / f"{i}.md").write_text(f"# {i}: {t}\n\n{b}\n", encoding="utf-8")
print(len(KB), "tệp KB đã ghi vào", D.resolve())
