import unittest

from apps.bilibili.collector import BILIBILI_FIXED_BVID, BILIBILI_FIXED_URL, BilibiliUi


def node(*, node_type="Text", text="", bounds="[0,0][10,10]", clickable="false", scrollable="false"):
    return {
        "attributes": {
            "id": "",
            "type": node_type,
            "text": text,
            "bounds": bounds,
            "visible": "true",
            "enabled": "true",
            "clickable": clickable,
            "scrollable": scrollable,
        },
        "children": [],
    }


class BilibiliUiContractTest(unittest.TestCase):
    def test_loaded_video_requires_observable_duration(self):
        layout = {
            "attributes": {"visible": "true"},
            "children": [
                node(text="00:12/00:24"),
                node(node_type="Stack", bounds="[64,1902][3056,1940]", clickable="true"),
                node(node_type="Column", bounds="[2343,151][3111,886]", clickable="true"),
            ],
        }
        state = BilibiliUi.state_from_layout(layout, app_running=True)
        self.assertTrue(state["video_loaded"])
        self.assertEqual(state["video_duration"], 24.0)
        self.assertEqual(state["playback_position"], 12.0)
        self.assertFalse(state["fullscreen"])

    def test_fixed_content_anchor_is_canonical_and_requires_visible_bvid_confirmation(self):
        self.assertEqual(BILIBILI_FIXED_URL, "https://www.bilibili.com/video/BV1enYL6SEtU/")
        self.assertEqual(BILIBILI_FIXED_BVID, "BV1enYL6SEtU")
        layout = {"attributes": {"visible": "true"}, "children": [node(text="BV1enYL6SEtU")]}
        self.assertEqual(BilibiliUi.state_from_layout(layout, app_running=True)["video_bvid"], BILIBILI_FIXED_BVID)

    def test_search_page_marker_is_observable_without_video_surface(self):
        layout = {
            "attributes": {"visible": "true"},
            "children": [node(node_type="TextInput", bounds="[100,100][900,160]") | {"attributes": {"id": "search.bar.input.key", "type": "TextInput", "text": "", "bounds": "[100,100][900,160]", "visible": "true", "enabled": "true", "clickable": "true"}}],
        }
        state = BilibiliUi.state_from_layout(layout, app_running=True)
        self.assertEqual(state["page_kind"], "search")
        self.assertTrue(state["search_page"])

    def test_comments_page_marker_is_observable(self):
        layout = {
            "attributes": {"visible": "true"},
            "children": [
                node(node_type="XComponent", bounds="[0,0][1000,1000]"),
                node(node_type="__Common__", bounds="[500,0][1000,1000]") | {"attributes": {"id": "ModalViewCommentArea", "type": "__Common__", "text": "", "bounds": "[500,0][1000,1000]", "visible": "true", "enabled": "true", "clickable": "false"}},
                node(text="热门评论"),
            ],
        }
        state = BilibiliUi.state_from_layout(layout, app_running=True)
        self.assertEqual(state["page_kind"], "comments")
        self.assertTrue(state["secondary_page"])


if __name__ == "__main__":
    unittest.main()
