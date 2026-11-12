"""The three agents of the workspace and the coordinator's delegation tools."""

from typing import Any, Dict, List

from agents.cms_tools import CMS_READ_TOOLS, CMS_WRITE_TOOLS, cms_status
from agents.content_tools import CONTENT_TOOLS, list_library_images
from agents.runtime import Agent

COORDINATOR_ID = "web-truyen"
CONTENT_ID = "content"
RESEARCH_ID = "research"

_COMMON = (
    "Luôn trả lời bằng tiếng Việt, ngắn gọn, rõ ràng. "
    "Khi dùng tool, tóm tắt kết quả cho người dùng (không dán JSON thô). "
    "Nếu tool báo lỗi, nói rõ lỗi và đề xuất bước tiếp theo."
)

CONTENT_INSTRUCTIONS = f"""Bạn là Agent Tạo content cho page Facebook/TikTok của một web truyện.
Nhiệm vụ: tạo ảnh và video Reels từ nội dung tóm tắt.
- Ảnh AI: generate_images_from_summary (từ tóm tắt) hoặc regenerate_image (từ ảnh có sẵn trong thư viện).
- Video: create_reel — chữ trên video chính là nội dung tóm tắt, ảnh nền là ảnh trong thư viện (ưu tiên ảnh 9:16).
- Ảnh đăng post: export_post_images (9:16 TikTok/Story, 4:5 Facebook post, 1:1).
- Gom file: zip_outputs.
Quy trình mặc định khi được giao "làm content từ tóm tắt X": tạo ảnh AI 9:16 + 4:5 → tạo reel với chữ = tóm tắt
→ xuất ảnh post → nén ZIP, rồi báo lại tên file. Nếu tạo ảnh AI lỗi (vd hết quota), dùng ảnh có sẵn
trong thư viện (list_library_images) và nói rõ điều đó. Không tự bịa image_id.
{_COMMON}"""

RESEARCH_INSTRUCTIONS = f"""Bạn là Agent Research content cho một web truyện (truyện chữ tiếng Việt).
Nhiệm vụ: tìm ý tưởng nội dung để quảng bá truyện trên Facebook/TikTok — chọn truyện nên đẩy, góc khai thác,
hook mở đầu, đoạn tóm tắt 2-4 câu dùng làm chữ trên Reels, gợi ý phong cách ảnh và hashtag.
Dùng dữ liệu thật từ CMS (list_stories, get_story, story_stats) trước khi đề xuất; nói rõ khi chỉ là giả định.
(Logic research chi tiết sẽ được bổ sung sau.)
{_COMMON}"""

COORDINATOR_INSTRUCTIONS = f"""Bạn là Agent quản lý Web truyện và là điều phối viên của workspace.
Bạn quản lý CMS truyện (xem tổng quan, tìm truyện, xem chi tiết, thống kê lượt xem; bật/tắt xuất bản, đề cử,
sửa tóm tắt) và giao việc cho 2 agent khác:
- ask_research_agent: nghiên cứu, chọn truyện, viết hook / tóm tắt cho content.
- ask_content_agent: tạo ảnh AI, video Reels, ảnh post, file ZIP.
Quy tắc chọn đường:
- Câu hỏi về truyện, chương, danh mục, xuất bản, đề cử, lượt xem → dùng tool CMS của bạn.
- Xem thư viện ảnh của Reels Studio (khác kho truyện) → tự gọi list_library_images.
- Tạo / sửa ảnh AI, video Reels, ảnh post, file ZIP → gọi ask_content_agent và chuyển kết quả cho người dùng.
- Ý tưởng, chọn truyện để quảng bá, hook, viết tóm tắt/caption → gọi ask_research_agent.
Khi giao việc, viết yêu cầu đầy đủ ngữ cảnh (tên truyện, tóm tắt, tỉ lệ ảnh, số lượng) vì agent kia
không thấy cuộc trò chuyện này. Với việc nhiều bước (vd "làm content cho truyện X"): lấy thông tin truyện
từ CMS → nhờ research viết tóm tắt/hook → giao content agent tạo ảnh + reels → báo kết quả.
Chỉ thay đổi dữ liệu CMS (xuất bản, đề cử, sửa tóm tắt) khi người dùng yêu cầu rõ ràng; với thay đổi
hàng loạt hãy liệt kê và hỏi xác nhận trước. Không có quyền xóa truyện/chương.
{_COMMON}"""


def _delegate(agent_id: str, task: str) -> Dict[str, Any]:
    agent = AGENTS[agent_id]
    reply = agent.chat(task, origin=AGENTS[COORDINATOR_ID].name)
    return {
        "agent": agent.name,
        "reply": reply["text"],
        "error": bool(reply.get("error")),
        "tools_used": [t["name"] for t in reply.get("tools") or []],
        "files": [a.get("filename") or a.get("url") for a in reply.get("artifacts") or []],
    }


def ask_content_agent(task: str) -> Dict[str, Any]:
    """Giao việc cho Agent Tạo content (tạo ảnh AI, video Reels, ảnh post, ZIP) và nhận kết quả.

    Args:
        task: yêu cầu đầy đủ ngữ cảnh, ví dụ tóm tắt cần làm, tỉ lệ ảnh, số lượng.
    """
    return _delegate(CONTENT_ID, task)


def ask_research_agent(task: str) -> Dict[str, Any]:
    """Giao việc cho Agent Research content (chọn truyện, góc khai thác, hook, tóm tắt cho Reels).

    Args:
        task: câu hỏi / yêu cầu nghiên cứu đầy đủ ngữ cảnh.
    """
    return _delegate(RESEARCH_ID, task)


AGENTS: Dict[str, Agent] = {
    COORDINATOR_ID: Agent(
        id=COORDINATOR_ID,
        name="Web truyện · Điều phối",
        role="Quản lý CMS truyện và giao việc cho các agent",
        instructions=COORDINATOR_INSTRUCTIONS,
        tools=[*CMS_READ_TOOLS, *CMS_WRITE_TOOLS, list_library_images, ask_research_agent, ask_content_agent],
        suggestions=[
            "Tổng quan CMS hôm nay thế nào?",
            "Top 5 truyện nhiều lượt xem 7 ngày qua",
            "Làm content Reels cho truyện được xem nhiều nhất tuần này",
        ],
        status_check=cms_status,
    ),
    CONTENT_ID: Agent(
        id=CONTENT_ID,
        name="Tạo content",
        role="Ảnh AI, video Reels, ảnh post Facebook/TikTok",
        instructions=CONTENT_INSTRUCTIONS,
        tools=list(CONTENT_TOOLS),
        suggestions=[
            "Thư viện ảnh đang có những ảnh nào?",
            "Tạo 1 ảnh 9:16 và 4:5 từ tóm tắt: Cô gái xuyên không về cổ đại, trở thành thần y",
            "Tạo reel từ ảnh mới nhất với chữ: Một đêm định mệnh thay đổi tất cả",
        ],
    ),
    RESEARCH_ID: Agent(
        id=RESEARCH_ID,
        name="Research content",
        role="Ý tưởng, hook, tóm tắt cho content (logic bổ sung sau)",
        instructions=RESEARCH_INSTRUCTIONS,
        tools=[*CMS_READ_TOOLS, list_library_images],
        suggestions=[
            "Gợi ý 3 truyện nên đẩy content tuần này và lý do",
            "Viết hook + tóm tắt 3 câu cho truyện mới cập nhật gần nhất",
        ],
        status_check=cms_status,
    ),
}


def agent_list() -> List[Dict[str, Any]]:
    return [
        {
            "id": a.id,
            "name": a.name,
            "role": a.role,
            "suggestions": a.suggestions,
            "tools": [t.__name__ for t in a.tools],
            "status": a.status(),
        }
        for a in AGENTS.values()
    ]
