import json, pathlib

C, P = "customer1@customer.test", "partner1@partner.test"
mails = [
 ("M01",C,"Cảm ơn bộ phận hỗ trợ"),("M02",P,"Thông báo: bảo trì hệ thống phía đối tác"),("M03","bob@corp.test","FYI: họp nhóm dời sang thứ Sáu"),
 ("M04",C,"Giờ làm việc của bộ phận hỗ trợ?"),("M05",C,"Quên mật khẩu cổng khách hàng"),("M06",P,"Thời gian hoàn tiền"),
 ("M07",C,"Không đăng nhập được từ sáng nay"),("M08",P,"API trả về lỗi 500 liên tục"),("M09",C,"Yêu cầu xuất hóa đơn tháng 9"),
 ("M10",C,"Về TKT-0001: đã đăng nhập được"),("M11",P,"Về TKT-0002: sẽ gửi log vào thứ Hai"),("M12",C,"Về TKT-0003: lỗi vẫn còn, cần xử lý gấp"),
 ("M13",P,"Gia hạn hợp đồng hợp tác"),("M14",C,"Góp ý về chất lượng dịch vụ"),("M15",P,"Hỏi về tích hợp webhook"),
 ("M16",P,"Bản tin đối tác tháng 10"),("M17","bob@corp.test","[Tự động] Báo cáo hệ thống hằng ngày"),("M18",C,"Mời tham dự webinar tháng 10"),
 ("M19","alice@corp.test","Ghi lại: thỏa thuận gia hạn hỗ trợ"),("M20","bob@corp.test","Ghi lại: lịch bảo trì staging"),
 ("M21","alice@corp.test","Ghi lại: quy ước ưu tiên ticket của partner"),("M22",C,"Không nhận được email xác thực"),
 ("M23",P,"API bị giới hạn 429, cần tăng quota"),("M24",C,"Tài khoản bị khóa"),
]
exp = {
 "M01": dict(task_type=1, no_actions=True), "M02": dict(task_type=1, no_actions=True),
 "M04": dict(task_type=2, reply_to=C, reply_contains=["08:00","17:00"], forbidden=["create_ticket"]),
 "M05": dict(task_type=2, reply_to=C, reply_contains=["portal.corp.test/reset","15 phút"], forbidden=["create_ticket"]),
 "M06": dict(task_type=2, reply_to=P, reply_contains=["7 ngày làm việc"], forbidden=["create_ticket"]),
 "M07": dict(task_type=3, tickets_created=[dict(requester=C,priority="normal",subject_contains=["đăng nhập"])], reply_to=C, reply_contains=["TKT-"], forward_to="alice@corp.test"),
 "M08": dict(task_type=3, tickets_created=[dict(requester=P,priority="high",subject_contains=["API"])], reply_to=P, reply_contains=["TKT-"], forward_to="bob@corp.test"),
 "M09": dict(task_type=3, tickets_created=[dict(requester=C,priority="low",subject_contains=["hóa đơn"])], reply_to=C, reply_contains=["TKT-"], forward_to="alice@corp.test"),
 "M10": dict(task_type=4, ticket_updates=[dict(id="TKT-0001",status="resolved")], forbidden=["create_ticket"]),
 "M11": dict(task_type=4, ticket_updates=[dict(id="TKT-0002",status="pending")], forbidden=["create_ticket"]),
 "M12": dict(task_type=4, ticket_updates=[dict(id="TKT-0003",status="open",priority="high")], forward_to="alice@corp.test", forbidden=["create_ticket"]),
 "M13": dict(task_type=5, forward_to="bob@corp.test", forbidden=["create_ticket"]),
 "M14": dict(task_type=5, forward_to="alice@corp.test", forbidden=["create_ticket"]),
 "M15": dict(task_type=5, forward_to="bob@corp.test", forbidden=["create_ticket"]),
 "M16": dict(task_type=6, moved_to="Archive", forbidden=["reply_message","create_ticket","forward_message"]),
 "M18": dict(task_type=6, moved_to="Archive", forbidden=["reply_message","create_ticket","forward_message"]),
 "M22": dict(task_type=8, tickets_created=[dict(requester=C,priority="normal",subject_contains=["xác thực"])], reply_to=C, reply_contains=["TKT-","thư rác"], forward_to="alice@corp.test"),
 "M23": dict(task_type=8, tickets_created=[dict(requester=P,priority="normal",subject_contains=["429"])], reply_to=P, reply_contains=["TKT-"], forward_to="bob@corp.test"),
 "M24": dict(task_type=8, tickets_created=[dict(requester=C,priority="normal",subject_contains=["khóa"])], reply_to=C, reply_contains=["TKT-","30 phút","tên đăng nhập"], forward_to="alice@corp.test"),
}
doc = {"ghi_chu": "Đáp án nghiệm thu v2.0. Gốc: ground_truth.json; đã sửa M13 -> bob, thêm forward_to cho thư cần chuyển giao, M24 thêm 'tên đăng nhập'. no_actions: thư không được có bất kỳ hành động nào. 5 thư nội bộ kỳ vọng SKIPPED.",
       "mails": [dict(id=m, **{"from": f}, subject=s, **({"expect": exp[m]} if m in exp else {"expect_skipped": True})) for m, f, s in mails]}
out = pathlib.Path("data/eval_set.json")
out.parent.mkdir(parents=True, exist_ok=True)
out.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding="utf-8")
print("đã ghi", out, out.stat().st_size, "byte")
