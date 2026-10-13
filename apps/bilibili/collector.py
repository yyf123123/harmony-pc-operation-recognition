#!/usr/bin/env python3
"""Collect UI-confirmed Bilibili VMA trials over HDC using one fixed BVID."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
import time
from pathlib import Path
from typing import Any, Callable

if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from apps.bilibili import BilibiliAdapter
from apps.preconditions import require_precondition
from apps.shared.collection_contract import cross_app_hdc_contract
from apps.shared.hdc_trial import (
    arm_reference_window,
    failure_code,
    feature_identifier,
    feature_payload,
    normalize_hdc_argument,
    now_id,
    snapshot,
    timestamp,
)
from apps.shared.hdc_ui import HdcLayoutUi
from collector.automation.base import BackendActionResult
from collector.automation.hdc_backend import HdcBackend, HdcError
from collector.reference_windows import CaptureProfile, ReferenceWindowCapture
from collector.schedule import balanced_trial_schedule
from collector.dataset.database import DatabaseStore
from collector.dataset.manifest import DatasetLayout
from collector.dataset.models import QualityRecord, SessionRecord, TrialRecord
from features.feature_schema import CROSS_APP_FEATURE_SCHEMA_VERSION
from features.vma_feature import build_cross_app_feature_pair
from collector.runtime.timers import TimingRecorder
from collector.runtime.trial_state import TrialState, TrialStateMachine


DEFAULT_OPERATIONS = (
    "bilibili.video.open_fixed_video",
    "bilibili.video.open_comments",
    "bilibili.video.to_background",
    "bilibili.social.open_search",
    "bilibili.social.open_uploader_profile",
    "bilibili.video.back_to_feed",
)

BILIBILI_FIXED_BVID = "BV1enYL6SEtU"
BILIBILI_FIXED_URL = f"https://www.bilibili.com/video/{BILIBILI_FIXED_BVID}/"
COLLECTION_CONTRACT = cross_app_hdc_contract("bilibili")


def _bounds(value: str) -> tuple[int, int, int, int]:
    try:
        left_top, right_bottom = value.strip("[]").split("][")
        left, top = (int(item) for item in left_top.split(","))
        right, bottom = (int(item) for item in right_bottom.split(","))
        return left, top, right, bottom
    except (AttributeError, ValueError):
        return 0, 0, 0, 0


def _area(node: dict[str, str]) -> int:
    left, top, right, bottom = _bounds(node.get("bounds", ""))
    return max(right - left, 0) * max(bottom - top, 0)


def _timecode(value: str) -> tuple[float, float] | None:
    match = re.fullmatch(r"\s*(\d{1,3}):(\d{2})\s*/\s*(\d{1,3}):(\d{2})\s*", value)
    if not match:
        return None
    current = int(match.group(1)) * 60 + int(match.group(2))
    duration = int(match.group(3)) * 60 + int(match.group(4))
    return float(current), float(duration)


class BilibiliUi(HdcLayoutUi):
    """Observable UI boundary for the current HarmonyOS Bilibili build."""

    layout_operation_name = "Bilibili layout dump"

    def __init__(self, backend: HdcBackend, bundle: str) -> None:
        # Reuse the shared layout/node helpers without a Browser URL contract.
        self.backend = backend
        self.bundle = bundle
        self.last_observed_duration: float | None = None
        self.fixed_bvid = BILIBILI_FIXED_BVID
        self.fixed_url = BILIBILI_FIXED_URL
        # The current device feed does not expose a stable BVID in every
        # accessibility layout.  Keep the observed content identity at the
        # session boundary instead of routing to a guessed/canonical URL.
        self.active_content_anchor: str | None = None
        self.active_content_signature: str | None = None

    @staticmethod
    def state_from_layout(layout: dict[str, Any], *, app_running: bool) -> dict[str, Any]:
        nodes = HdcLayoutUi.nodes(layout)
        visible = [node for node in nodes if node.get("visible") == "true"]
        root = next((node for node in visible if node.get("type") == "root"), None)
        has_observable_root = root is not None
        root_bounds = _bounds(root.get("bounds", "") if root else "")
        if root_bounds == (0, 0, 0, 0):
            root_bounds = (0, 0, 3120, 2080)
        root_left, root_top, root_right, root_bottom = root_bounds
        root_width = max(root_right - root_left, 1)
        root_height = max(root_bottom - root_top, 1)

        texts = [node.get("text", "") for node in visible if node.get("text")]
        node_strings = [
            value
            for node in visible
            for value in (node.get("text", ""), node.get("description", ""), node.get("originalText", ""), node.get("accessibilityId", ""))
            if value
        ]
        bvid_match = next((match.group(0) for value in node_strings if (match := re.search(r"BV[0-9A-Za-z]{10}", value))), None)
        error_text = " ".join(texts).lower()
        network_error = any(marker in error_text for marker in ("err_", "timed out", "connection refused", "can't reach", "网络错误", "无法加载"))
        timecodes = [_timecode(value) for value in texts]
        timecodes = [item for item in timecodes if item is not None]
        current_seconds, duration_seconds = timecodes[0] if timecodes else (None, None)

        def node_bounds(node: dict[str, str]) -> tuple[int, int, int, int]:
            return _bounds(node.get("bounds", ""))

        progress_bars = [
            node for node in visible
            if node.get("type") == "Stack" and node.get("clickable") == "true"
            and node_bounds(node)[2] - node_bounds(node)[0] > root_width * 0.35
            and node_bounds(node)[3] > root_top + root_height * 0.65
        ]
        progress_bar = max(progress_bars, key=_area, default=None)
        video_surface_present = any(
            node.get("type") == "XComponent" and _area(node) > 100_000
            for node in visible
        )

        related_panel = any(
            node.get("scrollable") == "true" and node.get("type") in {"List", "Swiper"}
            and node_bounds(node)[0] > root_left + root_width * 0.55
            for node in visible
        )
        home_cards = [
            node for node in visible
            if node.get("type") == "Column" and node.get("clickable") == "true"
            and 400 <= node_bounds(node)[2] - node_bounds(node)[0] <= root_width * 0.35
            and 350 <= node_bounds(node)[3] - node_bounds(node)[1] <= root_height * 0.6
            and node_bounds(node)[1] >= root_top + root_height * 0.08
            and node_bounds(node)[1] <= root_top + root_height * 0.75
        ]
        related_cards = [
            node for node in visible
            if node.get("type") == "Column" and node.get("clickable") == "true"
            and node_bounds(node)[0] > root_left + root_width * 0.55
            and 120 <= node_bounds(node)[3] - node_bounds(node)[1] <= 260
            and node_bounds(node)[1] >= root_top + root_height * 0.20
        ]

        # These page markers are deliberately based on device-observable
        # structure, not localized labels.  Search exposes a stable input id;
        # the uploader page is an embedded author-space Web surface; comments
        # expose a dedicated comment-area container and composer.  Keeping
        # these facts in state lets action confirmation reject a successful
        # click that did not actually navigate.
        search_page = bool(
            any(node.get("id") == "search.bar.input.key" for node in visible)
            and not video_surface_present
        )
        profile_page = bool(
            any(
                node.get("type") == "Web"
                and "hilo.bilibili.com/author_space" in (node.get("text") or "")
                for node in visible
            )
            or any(
                "space-header-bg" in (node.get("text") or "")
                for node in visible
            )
        )
        comments_page = bool(
            any(node.get("id") == "ModalViewCommentArea" for node in visible)
            or any(node.get("text") in {"热门评论", "评论千万条，等你发一条"} for node in visible)
        )

        title_candidates = []
        for node in visible:
            if node.get("type") != "Text" or not node.get("text"):
                continue
            left, top, right, bottom = node_bounds(node)
            if right - left >= root_width * 0.18 and bottom - top >= 20 and not _timecode(node.get("text", "")):
                title_candidates.append(node)
        title_node = max(title_candidates, key=lambda node: (_area(node), len(node.get("text", ""))), default=None)
        video_title = title_node.get("text", "") if title_node else ""

        # Some current Bilibili layouts render the playing XComponent and
        # related/title content while omitting the native progress overlay
        # from the accessibility tree.  Treat that as loaded only when both
        # the player surface and content evidence are present.  Duration is
        # still recorded when exposed and remains required by operations such
        # as seek that need a numeric playback target.
        duration_loaded = bool(duration_seconds and duration_seconds > 0 and progress_bar is not None)
        surface_content_loaded = bool(video_surface_present and (video_title or related_cards))
        video_loaded = duration_loaded or surface_content_loaded

        # A partial/mock layout may omit the root node and otherwise look like
        # a full-screen video.  Do not confirm fullscreen without the actual
        # device root plus a screen-sized video component.
        video_components = [
            node for node in visible
            if node.get("type") == "XComponent"
            and _area(node) >= root_width * root_height * 0.75
        ]
        fullscreen = bool(
            has_observable_root
            and video_loaded
            and not related_panel
            and root_left < 100
            and root_top < 100
            and video_components
        )

        related_text = []
        for node in visible:
            left, top, right, bottom = node_bounds(node)
            if left > root_left + root_width * 0.55 and node.get("text"):
                related_text.append(f"{node.get('text')}@{left},{top},{right},{bottom}")
        content_signature = hashlib.sha256(
            "|".join([
                video_title,
                str(duration_seconds),
                *(related_text or texts),
            ]).encode("utf-8", errors="replace")
        ).hexdigest()[:16]
        feed_ready = bool(
            len(home_cards) >= 1
            and not video_loaded
            and not video_surface_present
            and any(node.get("id") == "home-indicator-list-id" for node in visible)
        )
        if search_page:
            page_kind = "search"
        elif profile_page:
            page_kind = "profile"
        elif comments_page:
            page_kind = "comments"
        elif video_loaded or video_surface_present:
            page_kind = "video"
        elif feed_ready:
            page_kind = "feed"
        else:
            page_kind = "other"
        secondary_page = page_kind in {"comments", "search", "profile"}
        network_available = bool(
            app_running
            and not network_error
            and (video_loaded or len(home_cards) >= 1 or search_page or profile_page or comments_page)
        )
        return {
            "app_running": bool(app_running),
            "foreground": bool(visible and app_running),
            "network_available": network_available,
            "network_error": network_error,
            "video_loaded": video_loaded,
            "video_load_evidence": "duration_and_progress" if duration_loaded else ("surface_and_content" if surface_content_loaded else "none"),
            "video_surface_present": video_surface_present,
            "video_duration": duration_seconds,
            "playback_position": current_seconds,
            "fullscreen": fullscreen,
            "foreground_state": "foreground" if visible and app_running else "background",
            "home_card_count": len(home_cards),
            "related_card_count": len(related_cards),
            "second_video_available": len(related_cards) >= 1 or len(home_cards) >= 2,
            "feed_ready": feed_ready,
            "search_page": search_page,
            "profile_page": profile_page,
            "comments_page": comments_page,
            "secondary_page": secondary_page,
            "page_kind": page_kind,
            "video_title": video_title,
            "video_bvid": bvid_match,
            "content_signature": content_signature,
            "layout_node_count": len(nodes),
        }

    def state(self, token: str) -> dict[str, Any]:
        state = self.state_from_layout(self.layout(token), app_running=True)
        state["layout_token"] = token
        return state

    def wait_for(self, predicate: Callable[[dict[str, Any]], bool], *, timeout_s: float = 20.0, label: str) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.state(now_id("bilibili_layout"))
            if predicate(last):
                return last
            time.sleep(0.5)
        raise RuntimeError(f"Bilibili confirmation timeout: {label}; last_state={last}")

    @staticmethod
    def _sorted_nodes(nodes: list[dict[str, str]]) -> list[dict[str, str]]:
        return sorted(nodes, key=lambda node: (_bounds(node.get("bounds", ""))[1], _bounds(node.get("bounds", ""))[0]))

    def _click_first_feed_card(self, token: str) -> BackendActionResult:
        state = self.state(token)
        layout = self.layout(token)
        nodes = [node for node in self.nodes(layout) if node.get("visible") == "true"]
        root = next((node for node in nodes if node.get("id") == "bilibili_root_navigation"), None)
        root = root or next((node for node in nodes if node.get("type") == "root"), None)
        left, top, right, bottom = _bounds(root.get("bounds", "") if root else "[0,0][3120,2080]")
        width = max(right - left, 1)
        cards = [
            node for node in nodes
            if node.get("type") == "Column" and node.get("clickable") == "true"
            and 400 <= _bounds(node.get("bounds", ""))[2] - _bounds(node.get("bounds", ""))[0] <= width * 0.35
            and 350 <= _bounds(node.get("bounds", ""))[3] - _bounds(node.get("bounds", ""))[1] <= 1300
            and _bounds(node.get("bounds", ""))[1] >= top + 100
        ]
        cards = self._sorted_nodes(cards)
        if not cards:
            raise RuntimeError(f"no observable Bilibili feed video card; state={state}")
        card = cards[0]
        x1, y1, x2, y2 = _bounds(card["bounds"])
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str((x1 + x2) // 2), str((y1 + y2) // 2)))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili open feed video")
        after = self._wait_for_video_loaded(label="video loaded with duration")
        return BackendActionResult("pass", after, {
            "action": "open_video",
            "selection_strategy": "first_observable_feed_video",
            "card_bounds": card.get("bounds"),
            "before_signature": state.get("content_signature"),
            "observed_video_title": after.get("video_title"),
            "observed_duration": after.get("video_duration"),
            "observed_signature": after.get("content_signature"),
        }, source="hdc", is_mock=False)

    def _tap_player_surface(self, token: str) -> None:
        layout = self.layout(token)
        nodes = [node for node in self.nodes(layout) if node.get("visible") == "true"]
        root = next((node for node in nodes if node.get("id") == "bilibili_root_navigation"), None)
        root = root or next((node for node in nodes if node.get("type") == "root"), None)
        root_left, root_top, root_right, root_bottom = _bounds(root.get("bounds", "") if root else "[0,0][3120,2080]")
        root_width = max(root_right - root_left, 1)
        root_height = max(root_bottom - root_top, 1)

        play_text_nodes = [
            node for node in nodes
            if node.get("type") == "Text"
            and node.get("text")
            and _bounds(node.get("bounds", ""))[0] >= root_left + root_width * 0.55
            and _bounds(node.get("bounds", ""))[2] <= root_left + root_width * 0.70
            and root_top + root_height * 0.35 <= _bounds(node.get("bounds", ""))[1] <= root_top + root_height * 0.65
            and _timecode(node.get("text", "")) is None
        ]
        if play_text_nodes:
            # The pre-play page exposes a visible "立即播放"-like label, but
            # device decoding may make its text unreadable. Geometry is the
            # stable contract; click the label itself, not a parent surface.
            prompt = max(play_text_nodes, key=lambda node: (_bounds(node.get("bounds", ""))[1], _area(node)))
            left, top, right, bottom = _bounds(prompt.get("bounds", ""))
            result = self.backend.run(("shell", "uitest", "uiInput", "click", str((left + right) // 2), str((top + bottom) // 2)))
            if not result.ok:
                self.backend._raise_for_result(result, operation="Bilibili start observable video")
            return

        def descendant_text(node: dict[str, Any]) -> bool:
            attrs = node.get("attributes", {})
            if attrs.get("type") == "Text" and attrs.get("visible") == "true" and attrs.get("text"):
                return True
            return any(descendant_text(child) for child in node.get("children", []) or [])

        prompt_candidates: list[dict[str, str]] = []

        def find_prompt(node: dict[str, Any]) -> None:
            attrs = node.get("attributes", {})
            left, top, right, bottom = _bounds(attrs.get("bounds", ""))
            width = right - left
            height = bottom - top
            if (
                attrs.get("visible") == "true"
                and attrs.get("clickable") == "true"
                and attrs.get("type") in {"Row", "Column", "Stack"}
                and descendant_text(node)
                and width >= root_width * 0.5
                and 70 <= height <= 400
                and root_top + root_height * 0.3 <= top <= root_top + root_height * 0.75
            ):
                prompt_candidates.append(attrs)
            for child in node.get("children", []) or []:
                find_prompt(child)

        find_prompt(layout)
        if prompt_candidates:
            prompt = min(prompt_candidates, key=_area)
            left, top, right, bottom = _bounds(prompt.get("bounds", ""))
            result = self.backend.run(("shell", "uitest", "uiInput", "click", str((left + right) // 2), str((top + bottom) // 2)))
            if not result.ok:
                self.backend._raise_for_result(result, operation="Bilibili start observable video")
            return

        surfaces = [node for node in nodes if node.get("type") == "XComponent" and _area(node) > 100_000]
        if not surfaces:
            raise RuntimeError("Bilibili player surface is not observable")
        left, top, right, bottom = _bounds(max(surfaces, key=_area).get("bounds", ""))
        x = left + max(int((right - left) * 0.4), 1)
        y = top + max(int((bottom - top) * 0.45), 1)
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str(x), str(y)))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili reveal player controls")

    def _wait_for_video_loaded(
        self,
        *,
        label: str,
        timeout_s: float = 30.0,
        extra_predicate: Callable[[dict[str, Any]], bool] | None = None,
    ) -> dict[str, Any]:
        deadline = time.monotonic() + timeout_s
        tapped_surface = False
        last: dict[str, Any] = {}
        while time.monotonic() < deadline:
            last = self.state(now_id("bilibili_video_ready"))
            if last.get("video_loaded") and (extra_predicate is None or extra_predicate(last)):
                if last.get("video_duration") is not None:
                    self.last_observed_duration = float(last["video_duration"])
                return last
            # The player fades its controls quickly.  One surface tap is a
            # state-observation aid: it reveals the controls and may pause the
            # video, after which duration/position can be confirmed.
            if not tapped_surface and time.monotonic() >= deadline - timeout_s + 1.0:
                self._tap_player_surface(last["layout_token"])
                tapped_surface = True
            time.sleep(0.5)
        raise RuntimeError(f"Bilibili confirmation timeout: {label}; last_state={last}")

    def _video_controls(self, token: str) -> tuple[dict[str, str], dict[str, str]]:
        layout = self.layout(token)
        nodes = [node for node in self.nodes(layout) if node.get("visible") == "true" and node.get("clickable") == "true"]
        progress = [node for node in nodes if node.get("type") == "Stack" and _bounds(node.get("bounds", ""))[2] - _bounds(node.get("bounds", ""))[0] > 500 and _bounds(node.get("bounds", ""))[1] > 900]
        if not progress:
            state = self.state(token)
            if not state.get("video_surface_present"):
                raise RuntimeError("Bilibili progress bar is not observable")
            self._tap_player_surface(token)
            time.sleep(0.15)
            layout = self.layout(now_id("bilibili_controls"))
            nodes = [node for node in self.nodes(layout) if node.get("visible") == "true" and node.get("clickable") == "true"]
            progress = [node for node in nodes if node.get("type") == "Stack" and _bounds(node.get("bounds", ""))[2] - _bounds(node.get("bounds", ""))[0] > 500 and _bounds(node.get("bounds", ""))[1] > 900]
        if not progress:
            # The native player is an XComponent: when its overlay fades out,
            # uitest omits the overlay nodes. PlayerArea is still exposed and
            # its device-validated relative margins are stable across the
            # windowed and fullscreen layouts.
            player_areas = [node for node in self.nodes(layout) if node.get("visible") == "true" and node.get("id") == "PlayerArea"]
            if not player_areas:
                raise RuntimeError("Bilibili progress bar is not observable")
            area = max(player_areas, key=_area)
            left, top, right, bottom = _bounds(area.get("bounds", ""))
            progress = [{
                "type": "Stack",
                "clickable": "true",
                "bounds": f"[{left + 64},{bottom - 137}][{right - 64},{bottom - 103}]",
            }]
        progress_node = max(progress, key=_area)
        pleft, ptop, pright, pbottom = _bounds(progress_node["bounds"])
        full = [
            node for node in nodes
            if node.get("type") == "Image" and _bounds(node.get("bounds", ""))[0] > pleft + (pright - pleft) * 0.55
            and _bounds(node.get("bounds", ""))[1] > ptop
        ]
        if not full:
            player_areas = [node for node in self.nodes(layout) if node.get("visible") == "true" and node.get("id") == "PlayerArea"]
            if not player_areas:
                raise RuntimeError("Bilibili fullscreen control is not observable")
            area = max(player_areas, key=_area)
            left, top, right, bottom = _bounds(area.get("bounds", ""))
            full = [{
                "type": "Image",
                "clickable": "true",
                "bounds": f"[{right - 129},{bottom - 88}][{right - 45},{bottom - 4}]",
            }]
        fullscreen_node = max(full, key=lambda node: _bounds(node.get("bounds", ""))[0])
        return progress_node, fullscreen_node

    def _stabilize_paused(self) -> None:
        before = self.state(now_id("bilibili_still_before"))
        if not before.get("video_loaded"):
            return
        time.sleep(1.0)
        after = self.state(now_id("bilibili_still_after"))
        current_before = before.get("playback_position")
        current_after = after.get("playback_position")
        if current_before is None or current_after is None or current_after - current_before <= 0.5:
            return
        nodes = [node for node in self.nodes(self.layout(now_id("bilibili_pause_layout"))) if node.get("visible") == "true" and node.get("clickable") == "true" and node.get("type") == "Image"]
        candidates = [node for node in nodes if _bounds(node.get("bounds", ""))[1] > 900]
        if candidates:
            node = min(candidates, key=lambda item: _bounds(item.get("bounds", ""))[0])
            x1, y1, x2, y2 = _bounds(node["bounds"])
            click_x, click_y = (x1 + x2) // 2, (y1 + y2) // 2
        else:
            # Harmony's native XComponent can hide the Image overlay while
            # still exposing PlayerArea.  Use a single, device-validated
            # lower-left control location only when PlayerArea is observable;
            # the post-click playback-position check below is mandatory and
            # rejects any click that did not actually pause the video.
            areas = [
                node for node in self.nodes(self.layout(now_id("bilibili_pause_player_area")))
                if node.get("visible") == "true" and node.get("id") == "PlayerArea"
            ]
            if not areas:
                raise RuntimeError("video is playing and pause control is not observable")
            area = max(areas, key=_area)
            left, top, right, bottom = _bounds(area.get("bounds", ""))
            width, height = right - left, bottom - top
            if width < 300 or height < 180:
                raise RuntimeError(f"Bilibili PlayerArea is too small for pause fallback: {area.get('bounds')}")
            click_x = left + max(int(width * 0.075), 56)
            click_y = bottom - max(int(height * 0.105), 56)
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str(click_x), str(click_y)))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili pause for baseline")
        time.sleep(0.8)
        pause_check_before = self.state(now_id("bilibili_pause_confirm_before"))
        time.sleep(1.0)
        pause_check_after = self.state(now_id("bilibili_pause_confirm_after"))
        paused_at = pause_check_before.get("playback_position")
        paused_after = pause_check_after.get("playback_position")
        if paused_at is None or paused_after is None or float(paused_after) - float(paused_at) > 0.5:
            raise RuntimeError(
                "Bilibili pause action was not confirmed by settled playback position; "
                f"before={paused_at}, after={paused_after}"
            )

    def ensure_home(self) -> dict[str, Any]:
        state = self.state(now_id("bilibili_home_check"))
        if state.get("home_card_count", 0) >= 1 and not state.get("video_loaded") and not state.get("video_surface_present"):
            return state
        if state.get("secondary_page") and not state.get("video_surface_present"):
            # Search and uploader profile are secondary pages without the
            # video surface.  Return only after the feed structure is visible;
            # a successful key event alone is not a valid recovery.
            for _ in range(3):
                current = self.state(now_id("bilibili_secondary_back_check"))
                if current.get("home_card_count", 0) >= 1 and current.get("feed_ready"):
                    return current
                result = self.backend.run(("shell", "uitest", "uiInput", "keyEvent", "2"))
                if not result.ok:
                    self.backend._raise_for_result(result, operation="Bilibili secondary-page recovery back")
                try:
                    return self.wait_for(
                        lambda value: value.get("home_card_count", 0) >= 1 and value.get("feed_ready"),
                        timeout_s=4.0,
                        label="Bilibili feed after secondary-page recovery",
                    )
                except RuntimeError:
                    continue
            raise RuntimeError("Bilibili secondary page did not recover to observable feed")
        if state.get("video_loaded") or state.get("related_card_count", 0) >= 1 or state.get("video_surface_present"):
            deadline = time.monotonic() + 15.0
            while time.monotonic() < deadline:
                current = self.state(now_id("bilibili_back_check"))
                if current.get("home_card_count", 0) >= 1 and not current.get("video_loaded") and not current.get("video_surface_present"):
                    return current
                nodes = [node for node in self.nodes(self.layout(current["layout_token"])) if node.get("visible") == "true" and node.get("clickable") == "true"]
                root = next((node for node in nodes if node.get("type") == "root"), None)
                root_left, root_top, _, _ = _bounds(root.get("bounds", "") if root else "[0,0][3120,2080]")
                candidates = [
                    node for node in nodes
                    if node.get("type") == "Image"
                    and _bounds(node.get("bounds", ""))[0] < root_left + 120
                    and _bounds(node.get("bounds", ""))[1] < root_top + 180
                ]
                if candidates:
                    node = min(candidates, key=lambda item: _bounds(item.get("bounds", ""))[0])
                    x1, y1, x2, y2 = _bounds(node["bounds"])
                    result = self.backend.run(("shell", "uitest", "uiInput", "click", str((x1 + x2) // 2), str((y1 + y2) // 2)))
                    if not result.ok:
                        self.backend._raise_for_result(result, operation="Bilibili return to feed")
                    try:
                        return self.wait_for(lambda value: value.get("home_card_count", 0) >= 1 and not value.get("video_loaded") and not value.get("video_surface_present"), timeout_s=5.0, label="Bilibili feed home")
                    except RuntimeError:
                        # Some builds expose the back icon but route the first
                        # click to the video surface. Use system back only as
                        # a recovery action, and still require home evidence.
                        break
                time.sleep(0.5)
            for _ in range(2):
                result = self.backend.run(("shell", "uitest", "uiInput", "keyEvent", "2"))
                if not result.ok:
                    self.backend._raise_for_result(result, operation="Bilibili recovery back")
                try:
                    return self.wait_for(lambda value: value.get("home_card_count", 0) >= 1 and not value.get("video_loaded") and not value.get("video_surface_present"), timeout_s=6.0, label="Bilibili feed home after recovery back")
                except RuntimeError:
                    continue
            raise RuntimeError("Bilibili video is active but back control is not observable")
        # A prior to_background trial minimizes the PC window while keeping
        # the process alive.  -W is required on this build to restore the
        # foreground window; aa start without it can return success while the
        # layout remains the desktop/empty root.
        result = self.backend.run(("shell", "aa", "start", "-b", self.bundle, "-a", "EntryAbility", "-W"))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili launch")
        try:
            return self.wait_for(
                lambda value: value.get("home_card_count", 0) >= 1
                and not value.get("video_loaded")
                and not value.get("video_surface_present"),
                timeout_s=6.0,
                label="Bilibili feed after launch",
            )
        except RuntimeError:
            # On this PC-mode device, aa start -W can succeed while a system
            # desktop extension remains on top and the scoped Bilibili layout
            # has only its root node.  The launcher wrapper has independently
            # confirmed that one system-back event restores the app window.
            # Apply that recovery only for this observable hidden-layout case,
            # then still require the regular feed evidence before continuing.
            restored = self.state(now_id("bilibili_restored_after_launch"))
            if not restored.get("foreground") and restored.get("layout_node_count", 0) <= 1:
                back = self.backend.run(("shell", "uitest", "uiInput", "keyEvent", "2"))
                if not back.ok:
                    self.backend._raise_for_result(back, operation="Bilibili launch overlay recovery back")
                return self.wait_for(
                    lambda value: value.get("home_card_count", 0) >= 1
                    and value.get("feed_ready")
                    and not value.get("video_loaded")
                    and not value.get("video_surface_present"),
                    timeout_s=8.0,
                    label="Bilibili feed after launch overlay recovery",
                )
            # aa start may instead restore the last video detail.  Reuse the
            # existing observable back recovery; do not treat the restored
            # detail layout as a valid home precondition.
            if restored.get("video_surface_present") or restored.get("related_card_count", 0) >= 1:
                return self.ensure_home()
            raise

    def open_first_video(self, *, token: str) -> BackendActionResult:
        return self._click_first_feed_card(token)

    def open_fixed_video(self, *, token: str) -> BackendActionResult:
        """Open the first observable feed video and bind this session to it.

        The feed is network-selected and the accessibility tree may omit the
        BVID.  A device-observable card, loaded player, title/duration when
        exposed, and the derived content signature form the confirmation
        contract.  This preserves real content identity without inventing a
        fixed URL or silently accepting a failed route.
        """
        before = self.state(token)
        if before.get("video_loaded") or before.get("video_surface_present") or not before.get("home_card_count"):
            before = self.ensure_home()
        if not before.get("home_card_count"):
            raise RuntimeError(f"Bilibili feed video anchor is not observable: {before}")
        result = self.open_first_video(token=before["layout_token"])
        after = dict(result.state)
        if not after.get("content_signature"):
            raise RuntimeError("Bilibili opened video has no content signature")
        self.active_content_anchor = str(after.get("video_bvid") or after["content_signature"])
        self.active_content_signature = str(after["content_signature"])
        return BackendActionResult(
            "pass",
            after,
            {
                "action": "open_fixed_video",
                "selection_strategy": "first_observable_feed_video_per_session",
                "content_anchor": self.active_content_anchor,
                "video_bvid": after.get("video_bvid"),
                "video_title": after.get("video_title"),
                "video_duration": after.get("video_duration"),
                "content_signature": after.get("content_signature"),
            },
            source="hdc",
            is_mock=False,
        )

    def switch_video(self, *, token: str) -> BackendActionResult:
        before = self.state(token)
        nodes = [node for node in self.nodes(self.layout(token)) if node.get("visible") == "true" and node.get("clickable") == "true"]
        root = next((node for node in nodes if node.get("type") == "root"), None)
        left, top, right, bottom = _bounds(root.get("bounds", "") if root else "[0,0][3120,2080]")
        width = max(right - left, 1)
        cards = [
            node for node in nodes
            if node.get("type") == "Column" and node.get("clickable") == "true"
            and _bounds(node.get("bounds", ""))[0] > left + width * 0.55
            and 120 <= _bounds(node.get("bounds", ""))[3] - _bounds(node.get("bounds", ""))[1] <= 260
            and _bounds(node.get("bounds", ""))[1] >= top + 300
        ]
        if not cards:
            raise RuntimeError("Bilibili related video is not observable")
        node = self._sorted_nodes(cards)[0]
        x1, y1, x2, y2 = _bounds(node["bounds"])
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str((x1 + x2) // 2), str((y1 + y2) // 2)))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili switch related video")
        after = self._wait_for_video_loaded(
            label="second video observable",
            extra_predicate=lambda value: value.get("content_signature") != before.get("content_signature"),
        )
        self.active_content_anchor = str(after.get("video_bvid") or after.get("content_signature") or "")
        self.active_content_signature = str(after.get("content_signature") or "")
        return BackendActionResult("pass", after, {"action": "switch_video", "selection_strategy": "first_observable_related_video", "card_bounds": node.get("bounds"), "before_signature": before.get("content_signature"), "after_signature": after.get("content_signature"), "content_anchor": self.active_content_anchor}, source="hdc", is_mock=False)

    def seek(self, *, token: str, fraction: float = 0.3) -> BackendActionResult:
        before = self.state(token)
        progress, _ = self._video_controls(token)
        if not progress.get("accessibilityId"):
            self._tap_player_surface(token)
            time.sleep(0.1)
        x1, y1, x2, y2 = _bounds(progress["bounds"])
        x = int(x1 + (x2 - x1) * fraction)
        y = (y1 + y2) // 2
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str(x), str(y)))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili seek")
        duration = float(before.get("video_duration") or self.last_observed_duration or 0.0)
        if duration <= 0:
            raise RuntimeError("Bilibili seek duration is no longer observable")
        target = duration * fraction
        after = self._wait_for_video_loaded(
            label="fixed playback position",
            extra_predicate=lambda value: value.get("playback_position") is not None and abs(float(value["playback_position"]) - target) <= max(2.0, float(value.get("video_duration") or duration) * 0.12),
        )
        return BackendActionResult("pass", after, {"action": "seek", "position_fraction": fraction, "target_seconds": target, "before_position": before.get("playback_position"), "after_position": after.get("playback_position")}, source="hdc", is_mock=False)

    def enter_fullscreen(self, *, token: str) -> BackendActionResult:
        before = self.state(token)
        _, fullscreen = self._video_controls(token)
        if not fullscreen.get("accessibilityId"):
            self._tap_player_surface(token)
            time.sleep(0.1)
        x1, y1, x2, y2 = _bounds(fullscreen["bounds"])
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str((x1 + x2) // 2), str((y1 + y2) // 2)))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili enter fullscreen")
        after = self.wait_for(lambda value: value.get("video_loaded") and value.get("fullscreen") and not value.get("network_error"), label="fullscreen active")
        return BackendActionResult("pass", after, {"action": "enter_fullscreen", "before_fullscreen": before.get("fullscreen"), "after_fullscreen": after.get("fullscreen")}, source="hdc", is_mock=False)

    def exit_fullscreen(self, *, token: str) -> BackendActionResult:
        before = self.state(token)
        if not before.get("fullscreen"):
            raise RuntimeError("Bilibili exit fullscreen requires observable fullscreen state")
        layout = self.layout(token)
        nodes = [node for node in self.nodes(layout) if node.get("visible") == "true" and node.get("clickable") == "true"]
        root = next((node for node in nodes if node.get("type") == "root"), None)
        root_left, root_top, _, _ = _bounds(root.get("bounds", "") if root else "[0,0][3120,2080]")
        candidates = [
            node for node in nodes
            if node.get("type") in {"Image", "Button"}
            and _bounds(node.get("bounds", ""))[0] <= root_left + 220
            and _bounds(node.get("bounds", ""))[1] <= root_top + 220
        ]
        if not candidates:
            raise RuntimeError("Bilibili exit-fullscreen control is not observable")
        node = min(candidates, key=lambda item: (_bounds(item.get("bounds", ""))[1], _bounds(item.get("bounds", ""))[0]))
        left, top, right, bottom = _bounds(node["bounds"])
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str((left + right) // 2), str((top + bottom) // 2)))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili exit fullscreen")
        after = self.wait_for(lambda value: value.get("video_loaded") and not value.get("fullscreen"), label="windowed video active")
        return BackendActionResult("pass", after, {"action": "exit_fullscreen", "before_fullscreen": True, "after_fullscreen": after.get("fullscreen")}, source="hdc", is_mock=False)

    def _playback_state(self, token: str, *, observation_s: float = 0.8) -> dict[str, Any]:
        before = self.state(token)
        before_position = before.get("playback_position")
        if before_position is None:
            return {**before, "playback_state": "unknown"}
        time.sleep(observation_s)
        after = self.state(now_id("bilibili_playback_observation"))
        after_position = after.get("playback_position")
        if after_position is None:
            return {**after, "playback_state": "unknown"}
        delta = float(after_position) - float(before_position)
        return {**after, "playback_state": "playing" if delta >= 0.4 else "paused", "playback_delta_s": delta}

    def _play_pause_control(self, token: str) -> dict[str, str]:
        layout = self.layout(token)
        nodes = [node for node in self.nodes(layout) if node.get("visible") == "true" and node.get("clickable") == "true" and node.get("type") in {"Image", "Button"}]
        progress, _ = self._video_controls(token)
        left, top, _, bottom = _bounds(progress["bounds"])
        candidates = [
            node for node in nodes
            if _bounds(node.get("bounds", ""))[0] < left
            and top - 160 <= _bounds(node.get("bounds", ""))[1] <= bottom + 80
        ]
        if not candidates:
            raise RuntimeError("Bilibili play/pause control is not observable")
        return min(candidates, key=lambda item: _bounds(item.get("bounds", ""))[0])

    def _toggle_playback(self, *, token: str, desired: str) -> BackendActionResult:
        before = self._playback_state(token)
        control = self._play_pause_control(token)
        left, top, right, bottom = _bounds(control["bounds"])
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str((left + right) // 2), str((top + bottom) // 2)))
        if not result.ok:
            self.backend._raise_for_result(result, operation=f"Bilibili {desired}")
        after = self._playback_state(now_id(f"bilibili_{desired}_state"))
        if after.get("playback_state") != desired:
            raise RuntimeError(f"Bilibili {desired} was not observably confirmed: {after}")
        return BackendActionResult("pass", after, {"action": desired, "before_playback_state": before.get("playback_state"), "after_playback_state": after.get("playback_state"), "playback_delta_s": after.get("playback_delta_s")}, source="hdc", is_mock=False)

    def play(self, *, token: str) -> BackendActionResult:
        return self._toggle_playback(token=token, desired="playing")

    def pause(self, *, token: str) -> BackendActionResult:
        return self._toggle_playback(token=token, desired="paused")

    def open_comments(self, *, token: str) -> BackendActionResult:
        before = self.state(token)
        def find_tabs() -> list[dict[str, str]]:
            layout = self.layout(now_id("bilibili_comments_tabs"))
            nodes = [node for node in self.nodes(layout) if node.get("visible") == "true" and node.get("clickable") == "true"]
            root = next((node for node in nodes if node.get("type") == "root"), None)
            left, top, right, _ = _bounds(root.get("bounds", "") if root else "[0,0][3120,2080]")
            width = max(right - left, 1)
            tabs = [
                node for node in nodes
                if node.get("type") == "Column"
                and left + width * 0.55 <= _bounds(node.get("bounds", ""))[0] <= left + width * 0.85
                and 30 <= _bounds(node.get("bounds", ""))[3] - _bounds(node.get("bounds", ""))[1] <= 100
                and top + 50 <= _bounds(node.get("bounds", ""))[1] <= top + 320
            ]
            return self._sorted_nodes(tabs)

        deadline = time.monotonic() + 10.0
        tabs: list[dict[str, str]] = []
        while time.monotonic() < deadline:
            tabs = find_tabs()
            if len(tabs) >= 2:
                break
            time.sleep(0.5)
        if len(tabs) < 2:
            # A build variant can expose the same two tabs as Row/Stack
            # instead of Column.  Recover only from an observable player
            # window and the device-validated right-side tab geometry; the
            # post-click content-signature confirmation remains mandatory.
            layout = self.layout(now_id("bilibili_comments_tabs_fallback"))
            visible = [node for node in self.nodes(layout) if node.get("visible") == "true" and node.get("clickable") == "true"]
            root = next((node for node in self.nodes(layout) if node.get("type") == "root"), None)
            left, top, right, _ = _bounds(root.get("bounds", "") if root else "[0,0][3120,2080]")
            width = max(right - left, 1)
            fallback = [
                node for node in visible
                if node.get("type") in {"Column", "Row", "Stack"}
                and _bounds(node.get("bounds", ""))[0] >= left + width * 0.60
                and top + 40 <= _bounds(node.get("bounds", ""))[1] <= top + 220
                and 20 <= _bounds(node.get("bounds", ""))[3] - _bounds(node.get("bounds", ""))[1] <= 80
                and 40 <= _bounds(node.get("bounds", ""))[2] - _bounds(node.get("bounds", ""))[0] <= 400
            ]
            tabs = self._sorted_nodes(fallback)
        if len(tabs) < 2:
            raise RuntimeError("Bilibili comments tab is not observable")
        tab = tabs[1]
        x1, y1, x2, y2 = _bounds(tab["bounds"])
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str((x1 + x2) // 2), str((y1 + y2) // 2)))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili open comments")
        after = self.wait_for(
            lambda value: value.get("comments_page")
            and value.get("content_signature") != before.get("content_signature"),
            label="comments panel content change",
        )
        return BackendActionResult("pass", after, {"action": "open_comments", "tab_bounds": tab.get("bounds"), "before_signature": before.get("content_signature"), "after_signature": after.get("content_signature")}, source="hdc", is_mock=False)

    def open_search(self, *, token: str) -> BackendActionResult:
        before = self.state(token)
        layout = self.layout(token)
        all_nodes = [node for node in self.nodes(layout) if node.get("visible") == "true"]
        nodes = [node for node in all_nodes if node.get("clickable") == "true"]
        root = next((node for node in all_nodes if node.get("id") == "bilibili_root_navigation"), None)
        root = root or next((node for node in all_nodes if node.get("type") == "root"), None)
        left, top, right, _ = _bounds(root.get("bounds", "") if root else "[0,0][3120,2080]")
        width = max(right - left, 1)
        candidates = [
            node for node in nodes
            if node.get("type") == "Row"
            and _bounds(node.get("bounds", ""))[1] <= top + 110
            and _bounds(node.get("bounds", ""))[0] >= left + width * 0.04
            and _bounds(node.get("bounds", ""))[2] - _bounds(node.get("bounds", ""))[0] >= width * 0.60
        ]
        if not candidates:
            raise RuntimeError("Bilibili feed search entry is not observable")
        entry = max(candidates, key=lambda node: _bounds(node.get("bounds", ""))[2] - _bounds(node.get("bounds", ""))[0])
        x1, y1, x2, y2 = _bounds(entry["bounds"])
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str((x1 + x2) // 2), str((y1 + y2) // 2)))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili open search")
        after = self.wait_for(
            lambda value: value.get("page_kind") == "search"
            and value.get("search_page")
            and value.get("content_signature") != before.get("content_signature"),
            label="search page visible",
        )
        return BackendActionResult(
            "pass",
            after,
            {"action": "open_search", "selection_strategy": "observable_feed_search_entry", "entry_bounds": entry.get("bounds"), "before_page_kind": before.get("page_kind"), "after_page_kind": after.get("page_kind")},
            source="hdc",
            is_mock=False,
        )

    def open_uploader_profile(self, *, token: str) -> BackendActionResult:
        before = self.state(token)
        layout = self.layout(token)
        all_nodes = [node for node in self.nodes(layout) if node.get("visible") == "true"]
        nodes = [node for node in all_nodes if node.get("clickable") == "true"]
        root = next((node for node in all_nodes if node.get("id") == "bilibili_root_navigation"), None)
        root = root or next((node for node in all_nodes if node.get("type") == "root"), None)
        left, top, right, _ = _bounds(root.get("bounds", "") if root else "[0,0][3120,2080]")
        width = max(right - left, 1)
        candidates = [
            node for node in nodes
            if node.get("type") == "Column"
            and _bounds(node.get("bounds", ""))[0] >= left + width * 0.68
            and top + 90 <= _bounds(node.get("bounds", ""))[1] <= top + 230
            and 250 <= _bounds(node.get("bounds", ""))[2] - _bounds(node.get("bounds", ""))[0] <= width * 0.35
            and 35 <= _bounds(node.get("bounds", ""))[3] - _bounds(node.get("bounds", ""))[1] <= 90
        ]
        if not candidates:
            raise RuntimeError("Bilibili uploader profile entry is not observable")
        entry = min(candidates, key=lambda node: (_bounds(node.get("bounds", ""))[1], -_area(node)))
        x1, y1, x2, y2 = _bounds(entry["bounds"])
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str((x1 + x2) // 2), str((y1 + y2) // 2)))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili open uploader profile")
        after = self.wait_for(
            lambda value: value.get("page_kind") == "profile"
            and value.get("profile_page")
            and value.get("content_signature") != before.get("content_signature"),
            label="uploader profile page visible",
        )
        return BackendActionResult(
            "pass",
            after,
            {"action": "open_uploader_profile", "selection_strategy": "observable_uploader_header", "entry_bounds": entry.get("bounds"), "before_page_kind": before.get("page_kind"), "after_page_kind": after.get("page_kind")},
            source="hdc",
            is_mock=False,
        )

    def back_to_feed(self, *, token: str) -> BackendActionResult:
        before = self.state(token)
        if before.get("page_kind") != "comments":
            raise RuntimeError(f"Bilibili back_to_feed requires confirmed comments page: {before}")
        result = self.backend.run(("shell", "uitest", "uiInput", "keyEvent", "2"))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili return from comments to feed")
        after = self.wait_for(
            lambda value: value.get("page_kind") == "feed"
            and value.get("feed_ready")
            and not value.get("video_surface_present"),
            label="feed after confirmed comments back",
        )
        return BackendActionResult(
            "pass",
            after,
            {"action": "back_to_feed", "start_page_kind": before.get("page_kind"), "after_page_kind": after.get("page_kind"), "back_method": "system_back_after_comments"},
            source="hdc",
            is_mock=False,
        )

    def to_background(self, *, token: str) -> BackendActionResult:
        before = self.state(token)
        nodes = [node for node in self.nodes(self.layout(token)) if node.get("visible") == "true" and node.get("clickable") == "true"]
        minimize = next((node for node in nodes if node.get("id") == "EnhanceMinimizeBtn"), None)
        if not minimize:
            raise RuntimeError("Bilibili minimize control is not observable")
        x1, y1, x2, y2 = _bounds(minimize["bounds"])
        result = self.backend.run(("shell", "uitest", "uiInput", "click", str((x1 + x2) // 2), str((y1 + y2) // 2)))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili background")
        after = self.wait_for(lambda value: value.get("foreground_state") == "background" and not value.get("foreground"), label="Bilibili background")
        return BackendActionResult("pass", after, {"action": "background", "before_foreground": before.get("foreground_state"), "after_foreground": after.get("foreground_state")}, source="hdc", is_mock=False)

    def fixed_scroll(self, *, token: str) -> BackendActionResult:
        before = self.state(token)
        if before.get("fullscreen"):
            raise RuntimeError("fixed scroll requires the windowed video page")
        result = self.backend.run(("shell", "uitest", "uiInput", "swipe", "2500", "1150", "2500", "650", "800"))
        if not result.ok:
            self.backend._raise_for_result(result, operation="Bilibili fixed scroll")
        after = self.wait_for(lambda value: value.get("video_surface_present") and value.get("content_signature") != before.get("content_signature"), label="related panel scroll")
        return BackendActionResult("pass", after, {"action": "fixed_scroll", "from": [2500, 1150], "to": [2500, 650], "before_signature": before.get("content_signature"), "after_signature": after.get("content_signature")}, source="hdc", is_mock=False)

    def prepare_for(self, operation_id: str) -> dict[str, Any]:
        initial = self.state(now_id("bilibili_prepare_initial"))
        if operation_id == "bilibili.video.open_fixed_video":
            if initial.get("video_loaded") or initial.get("video_surface_present") or initial.get("related_card_count", 0) or not initial.get("home_card_count"):
                return self.ensure_home()
            return initial
        if operation_id == "bilibili.control.settled_noop":
            # A randomized schedule may place the no-op immediately after
            # to_background. Restore the observable video page during
            # preparation so the measured action remains a genuine no-op.
            if initial.get("foreground") and initial.get("network_available") and initial.get("content_signature"):
                return initial
            return self.ensure_home()
        if operation_id == "bilibili.social.open_search":
            return self.ensure_home()
        self.open_fixed_video(token=now_id("bilibili_prepare_video"))
        self._stabilize_paused()
        prepared = self.state(now_id("bilibili_prepare_action"))
        if not prepared.get("video_loaded") and prepared.get("video_surface_present"):
            # Player controls are transient. Re-open them immediately before
            # checking the next operation's duration/fullscreen contract.
            prepared = self._wait_for_video_loaded(label="video controls after preparation", timeout_s=8.0)
        if operation_id == "bilibili.video.exit_fullscreen":
            if not prepared.get("fullscreen"):
                self.enter_fullscreen(token=prepared["layout_token"])
            prepared = self.state(now_id("bilibili_prepare_exit_fullscreen"))
        if operation_id in {"bilibili.video.play", "bilibili.video.pause"}:
            prepared = self._playback_state(prepared["layout_token"])
            desired_before = "paused" if operation_id == "bilibili.video.play" else "playing"
            if prepared.get("playback_state") != desired_before:
                toggle = self.pause if desired_before == "paused" else self.play
                toggle(token=prepared["layout_token"])
                prepared = self._playback_state(now_id("bilibili_prepare_playback"))
        if operation_id == "bilibili.video.back_to_feed":
            self.open_comments(token=prepared["layout_token"])
            prepared = self.state(now_id("bilibili_prepare_comments"))
        return prepared

    def action(self, operation_id: str, *, token: str) -> BackendActionResult:
        if operation_id == "bilibili.video.open_fixed_video":
            return self.open_fixed_video(token=token)
        if operation_id == "bilibili.control.settled_noop":
            return BackendActionResult("pass", self.state(now_id("bilibili_control_noop")), {"action": "noop"}, source="hdc", is_mock=False)
        if operation_id == "bilibili.video.switch_fixed_video":
            return self.switch_video(token=token)
        if operation_id == "bilibili.video.seek_fixed_position":
            return self.seek(token=token)
        if operation_id == "bilibili.video.enter_fullscreen":
            return self.enter_fullscreen(token=token)
        if operation_id == "bilibili.video.exit_fullscreen":
            return self.exit_fullscreen(token=token)
        if operation_id == "bilibili.video.open_comments":
            return self.open_comments(token=token)
        if operation_id == "bilibili.social.open_search":
            return self.open_search(token=token)
        if operation_id == "bilibili.social.open_uploader_profile":
            return self.open_uploader_profile(token=token)
        if operation_id == "bilibili.video.back_to_feed":
            return self.back_to_feed(token=token)
        if operation_id == "bilibili.video.to_background":
            return self.to_background(token=token)
        if operation_id == "bilibili.video.fixed_scroll":
            return self.fixed_scroll(token=token)
        if operation_id == "bilibili.video.play":
            return self.play(token=token)
        if operation_id == "bilibili.video.pause":
            return self.pause(token=token)
        raise ValueError(operation_id)

    @staticmethod
    def confirm(operation_id: str, before: dict[str, Any], action_state: dict[str, Any]) -> BackendActionResult:
        # BVID visibility is build-dependent; the content signature and
        # observable loaded player are the session-safe content contract.
        fixed_content = bool(action_state.get("video_loaded")) and bool(action_state.get("content_signature"))
        if operation_id == "bilibili.control.settled_noop":
            ok = action_state.get("app_running") and action_state.get("content_signature") == before.get("content_signature")
        elif operation_id == "bilibili.video.open_fixed_video":
            ok = fixed_content and bool(action_state.get("video_loaded"))
        elif operation_id == "bilibili.video.switch_fixed_video":
            ok = bool(action_state.get("video_loaded")) and action_state.get("content_signature") != before.get("content_signature")
        elif operation_id == "bilibili.video.seek_fixed_position":
            duration = float(before.get("video_duration", 0) or action_state.get("video_duration", 0) or 0)
            position = action_state.get("playback_position")
            ok = fixed_content and bool(action_state.get("video_loaded")) and position is not None and abs(float(position) - duration * 0.3) <= max(2.0, duration * 0.12)
        elif operation_id == "bilibili.video.enter_fullscreen":
            ok = fixed_content and bool(action_state.get("video_loaded")) and not bool(before.get("fullscreen")) and bool(action_state.get("fullscreen"))
        elif operation_id == "bilibili.video.exit_fullscreen":
            ok = fixed_content and bool(before.get("fullscreen")) and bool(action_state.get("video_loaded")) and not bool(action_state.get("fullscreen"))
        elif operation_id == "bilibili.video.open_comments":
            ok = bool(before.get("video_loaded")) and bool(action_state.get("comments_page")) and action_state.get("content_signature") != before.get("content_signature")
        elif operation_id == "bilibili.social.open_search":
            ok = (
                action_state.get("page_kind") == "search"
                and action_state.get("search_page")
                and not action_state.get("video_surface_present")
                and action_state.get("content_signature") != before.get("content_signature")
            )
        elif operation_id == "bilibili.social.open_uploader_profile":
            ok = (
                action_state.get("page_kind") == "profile"
                and action_state.get("profile_page")
                and not action_state.get("video_surface_present")
                and action_state.get("content_signature") != before.get("content_signature")
            )
        elif operation_id == "bilibili.video.back_to_feed":
            ok = (
                before.get("page_kind") == "comments"
                and action_state.get("page_kind") == "feed"
                and action_state.get("feed_ready")
                and not action_state.get("video_surface_present")
            )
        elif operation_id == "bilibili.video.to_background":
            ok = before.get("foreground_state") == "foreground" and action_state.get("foreground_state") == "background" and not action_state.get("foreground")
        elif operation_id == "bilibili.video.fixed_scroll":
            ok = fixed_content and bool(action_state.get("video_surface_present")) and action_state.get("content_signature") != before.get("content_signature")
        elif operation_id == "bilibili.video.play":
            ok = fixed_content and before.get("playback_state") == "paused" and action_state.get("playback_state") == "playing"
        elif operation_id == "bilibili.video.pause":
            ok = fixed_content and before.get("playback_state") == "playing" and action_state.get("playback_state") == "paused"
        else:
            ok = False
        event = {"operation_id": operation_id, "confirmed": bool(ok), "before": before, "after_action": action_state, "confirmation_method": "observable_layout_state"}
        return BackendActionResult("pass" if ok else "fail", action_state, event, source="hdc", is_mock=False)


def run_trial(
    backend: HdcBackend,
    ui: BilibiliUi,
    database: DatabaseStore,
    layout: DatasetLayout,
    adapter: BilibiliAdapter,
    operation_id: str,
    session_id: str,
    trial_id: str,
    round_index: int,
    *,
    capture_profile: CaptureProfile | None = None,
    sleep: Callable[[float], None] = time.sleep,
    schedule_position: int | None = None,
) -> dict[str, Any]:
    capture_profile = capture_profile or CaptureProfile()
    contract_metadata = COLLECTION_CONTRACT.to_metadata()
    operation = adapter.catalog.get(operation_id)
    if database.trial_exists(trial_id):
        raise ValueError(f"duplicate trial id: {trial_id}")
    machine = TrialStateMachine()
    timing = TimingRecorder(source="hdc", is_mock=False)
    started = timing.start_total()
    database.insert_trial(TrialRecord(trial_id, session_id, operation_id, "running", source="hdc", is_mock=False, current_state=machine.state.value, quality_status="INVALID"))
    captured: list[tuple[Any, Path, str]] = []
    quality_written = False
    expected_pid: str | None = None
    prepared: dict[str, Any] = {}
    baseline_ts: str | None = None
    action_start_ts: str | None = None
    action_end_ts: str | None = None
    post_ts: str | None = None
    action_duration_ms: float | None = None
    window_capture: ReferenceWindowCapture[Any] | None = None
    try:
        prepared = ui.prepare_for(operation_id)
        machine.advance(TrialState.PRECONDITION_CHECK)
        require_precondition(operation, prepared)
        database.insert_event(
            trial_id,
            "PRECONDITION_CHECK",
            timestamp(),
            {
                "operation_id": operation_id,
                "state": prepared,
                "confirmed": True,
                "capture_profile": capture_profile.to_metadata(),
                "collection_contract": contract_metadata,
            },
            source="hdc",
            is_mock=False,
        )

        # Keep the reference-window semantics identical to the Browser
        # collector when a caller selects the settled profile.  Preparation is
        # outside the measured action window and must be reconfirmed after it.
        if capture_profile.prepare_settle_ms:
            sleep(capture_profile.prepare_settle_ms / 1000.0)
            prepared = ui.state(now_id("bilibili_prepare_settle"))
            require_precondition(operation, prepared)
            database.insert_event(
                trial_id,
                "PREPARE_SETTLE",
                timestamp(),
                {
                    "operation_id": operation_id,
                    "duration_ms": capture_profile.prepare_settle_ms,
                    "state": prepared,
                    "confirmed": True,
                    "capture_profile": capture_profile.to_metadata(),
                    "collection_contract": contract_metadata,
                },
                source="hdc",
                is_mock=False,
            )

        def clear_current_process() -> None:
            nonlocal expected_pid
            expected_pid = arm_reference_window(backend, adapter.app_id, expected_pid, timing=timing)

        window_capture = ReferenceWindowCapture(clear_refs=clear_current_process, sleep=sleep)

        def capture_armed(phase: str) -> Any:
            nonlocal expected_pid, baseline_ts, post_ts
            item, path, expected_pid = window_capture.capture(
                phase,
                lambda: snapshot(
                    backend,
                    layout,
                    adapter.app_id,
                    trial_id,
                    phase,
                    expected_pid,
                    clear_before_capture=False,
                    timing=timing,
                ),
            )
            captured.append((item, path, expected_pid))
            database.insert_snapshot(item.snapshot_id, trial_id, item.phase, item.timestamp, str(path), source="hdc", is_mock=False)
            if phase == "BASELINE_1":
                baseline_ts = item.timestamp
            if phase == "POST_3000":
                post_ts = item.timestamp
            return item

        machine.advance(TrialState.BASELINE)
        for phase in capture_profile.baseline_phases:
            window_capture.arm(phase)
            window_capture.wait_ms(phase, capture_profile.baseline_window_ms)
            capture_armed(phase)
        machine.advance(TrialState.ACTION)
        before_action = ui.state(now_id("bilibili_before_action"))
        window_capture.arm(capture_profile.action_phase)
        action_started = time.perf_counter()
        action_start_ts = timestamp()
        action = ui.action(operation_id, token=before_action["layout_token"])
        action_end_ts = timestamp()
        action_duration_ms = (time.perf_counter() - action_started) * 1000.0
        if action.status == "needs_device_validation":
            raise HdcError("NEEDS_DEVICE_VALIDATION", str(action.event))
        if action.status != "pass":
            raise RuntimeError(f"Bilibili action returned {action.status}")
        machine.advance(TrialState.ACTION_CONFIRM)
        confirmation = ui.confirm(operation_id, before_action, dict(action.state))
        if confirmation.status != "pass":
            raise RuntimeError("observable action confirmation failed")
        database.insert_event(trial_id, "ACTION_CONFIRM", timestamp(), dict(confirmation.event), source="hdc", is_mock=False)
        action_item = capture_armed(capture_profile.action_phase)
        machine.advance(TrialState.POST_ACTION)
        post_items: list[Any] = []
        for phase, window_ms in capture_profile.post_windows:
            window_capture.arm(phase)
            window_capture.wait_ms(phase, window_ms)
            post_items.append(capture_armed(phase))
        machine.advance(TrialState.QUALITY_CHECK)
        with timing.measure("T_feature"):
            features = build_cross_app_feature_pair(
                sample_id=trial_id,
                app_id=adapter.app_id,
                operation_id=operation_id,
                baselines=[item for item, _, _ in captured[:2]],
                action=action_item,
                post_action=post_items[0],
                post_actions=post_items[1:],
                metadata={
                    "source": "hdc",
                    "is_mock": False,
                    "target": backend.target,
                    "pid": expected_pid,
                    "round": round_index,
                    "schedule_position": schedule_position,
                    "capture_profile": capture_profile.to_metadata(),
                    "collection_contract": contract_metadata,
                    "action_duration_ms": action_duration_ms,
                    "before_action": before_action,
                    "action_event": dict(action.event),
                    "confirmation_event": dict(confirmation.event),
                    "state_history": [item.value for item in machine.history],
                },
            )
        feature = features[0]
        if len(feature.raw_vector) != 2048 or len(features[1].raw_vector) != 8192 or any(item.signal_strength <= 0.0 for item in features):
            raise RuntimeError("feature has no usable signal")
        database.insert_event(trial_id, "QUALITY_CHECK", timestamp(), {"schema_signals": {item.schema_version: item.signal_strength for item in features}, "vector_dims": {item.schema_version: len(item.raw_vector) for item in features}}, source="hdc", is_mock=False)
        machine.advance(TrialState.SAVE)
        feature_paths: dict[str, str] = {}
        for item in features:
            feature_path = layout.write_derived_json("feature", feature_identifier(trial_id, item.schema_version), feature_payload(item))
            feature_paths[item.schema_version] = str(feature_path)
        database.insert_features(
            (trial_id, item.schema_version, feature_paths[item.schema_version], len(item.raw_vector), "hdc", False)
            for item in features
        )
        database.insert_quality(QualityRecord(trial_id, True, feature.signal_strength, source="hdc", is_mock=False, quality_status="VALID"))
        quality_written = True
        timing.finish_total(started)
        database.insert_timing(trial_id, timing.values, source="hdc", is_mock=False)
        database.finalize_trial_success(
            trial_id,
            state={"target": backend.target, "pid": expected_pid, "round": round_index, "schedule_position": schedule_position, "capture_profile": capture_profile.to_metadata(), "collection_contract": contract_metadata, "action_duration_ms": action_duration_ms, "window_events": window_capture.events, "state_history": [item.value for item in machine.history]},
            baseline_ts=baseline_ts,
            action_start_ts=action_start_ts,
            action_end_ts=action_end_ts,
            post_ts=post_ts,
        )
        return {"trial_id": trial_id, "operation_id": operation_id, "round": round_index, "schedule_position": schedule_position, "status": "success", "quality_status": "VALID", "signal_strength": feature.signal_strength, "feature_path": feature_paths[CROSS_APP_FEATURE_SCHEMA_VERSION], "feature_paths": feature_paths, "pid": expected_pid, "state_history": [item.value for item in machine.history], "timing": timing.values}
    except Exception as error:
        code = failure_code(error, machine.state)
        if machine.state != TrialState.SAVE:
            machine.fail()
        database.mark_trial_failed(trial_id, error_code=code.value, error_detail=str(error), state={"target": backend.target, "round": round_index, "schedule_position": schedule_position, "capture_profile": capture_profile.to_metadata(), "collection_contract": contract_metadata, "prepared": prepared, "pid": expected_pid, "window_events": window_capture.events if window_capture else [], "state_history": [item.value for item in machine.history]})
        if not quality_written:
            database.insert_quality(QualityRecord(trial_id, False, None, source="hdc", is_mock=False, quality_status="INVALID", error_code=code.value, reason=str(error)))
        database.insert_event(trial_id, "FAILED", timestamp(), {"error_code": code.value, "error_detail": str(error), "state_history": [item.value for item in machine.history], "window_events": window_capture.events if window_capture else [], "captured_snapshot_count": len(captured)}, source="hdc", is_mock=False)
        timing.finish_total(started)
        database.insert_timing(trial_id, timing.values, source="hdc", is_mock=False)
        return {"trial_id": trial_id, "operation_id": operation_id, "round": round_index, "status": "failed", "quality_status": "INVALID", "error_code": code.value, "error_detail": str(error), "state_history": [item.value for item in machine.history]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--target", required=True)
    parser.add_argument("--bundle", default="yylx.danmaku.bili")
    parser.add_argument("--process", default="yylx.danmaku.bili")
    parser.add_argument("--rounds", type=int, default=10)
    parser.add_argument("--operations", default=",".join(DEFAULT_OPERATIONS))
    parser.add_argument("--baseline-window-ms", type=int, default=750)
    parser.add_argument("--post-short-window-ms", type=int, default=750)
    parser.add_argument("--post-long-window-ms", type=int, default=3000)
    parser.add_argument("--prepare-settle-ms", type=int, default=0)
    parser.add_argument("--schedule-seed", type=int, default=20260914)
    parser.add_argument("--control-trials", type=int, default=10)
    parser.add_argument("--collection-stage", choices=("smoke", "pilot", "formal"), default="pilot")
    parser.add_argument("--formal-gate-report", type=Path)
    parser.add_argument("--root", default=".")
    parser.add_argument("--hdc", nargs="+", metavar="HDC")
    args = parser.parse_args(argv)
    if not 1 <= args.rounds <= 25:
        parser.error("--rounds must be between 1 and 25")
    operation_ids = tuple(item.strip() for item in args.operations.split(",") if item.strip())
    if not operation_ids:
        parser.error("at least one operation is required")
    if args.control_trials not in {0, args.rounds}:
        parser.error("--control-trials must be 0 or equal --rounds so no-op controls stay balanced")
    if min(args.baseline_window_ms, args.post_short_window_ms, args.post_long_window_ms, args.prepare_settle_ms) < 0:
        parser.error("capture window durations must not be negative")
    if args.collection_stage == "formal":
        if args.rounds != 20:
            parser.error("formal collection requires exactly 20 valid-trial attempts per frozen operation")
        if args.formal_gate_report is None or not args.formal_gate_report.exists():
            parser.error("formal collection requires an existing stable-set gate report via --formal-gate-report")
    capture_profile = CaptureProfile(
        name="cross_app.reference_windows.v2_settled" if args.prepare_settle_ms else "cross_app.reference_windows.v1",
        baseline_window_ms=args.baseline_window_ms,
        post_short_window_ms=args.post_short_window_ms,
        post_long_window_ms=args.post_long_window_ms,
        prepare_settle_ms=args.prepare_settle_ms,
    )
    scheduled_ids = (*operation_ids, "bilibili.control.settled_noop") if args.control_trials else operation_ids
    schedule = balanced_trial_schedule(scheduled_ids, rounds=args.rounds, seed=args.schedule_seed)

    backend = HdcBackend(hdc=normalize_hdc_argument(args.hdc), target=args.target)
    adapter = BilibiliAdapter()
    layout = DatasetLayout(args.root)
    database = DatabaseStore(Path(args.root) / "database/vma_workload.sqlite")
    session_id = now_id("bilibili_real_vma")
    results: list[dict[str, Any]] = []
    try:
        targets = backend.discover_targets()
        if targets != (args.target,):
            raise SystemExit(json.dumps({"status": "blocked", "error": "HDC target selection is not unique or changed", "targets": targets, "requested": args.target}, ensure_ascii=False))
        permissions = backend.check_permissions()
        backend.register_validated_app(adapter.app_id, bundle=args.bundle, process=args.process)
        pid = backend.ensure_process_alive(adapter.app_id)
        database.insert_catalog(adapter.catalog)
        database.insert_session(SessionRecord(session_id, adapter.app_id, timestamp(), "hdc", False, {"target": backend.target, "bundle": args.bundle, "process": args.process, "initial_pid": pid, "collection_stage": args.collection_stage, "rounds": args.rounds, "operation_ids": list(operation_ids), "control_operation_id": "bilibili.control.settled_noop" if args.control_trials else None, "control_trials": args.control_trials, "schedule_seed": args.schedule_seed, "schedule": [{"round": item.round_index, "position": item.position, "operation_id": item.operation_id} for item in schedule], "capture_profile": capture_profile.to_metadata(), "collection_contract": COLLECTION_CONTRACT.to_metadata(), "fixed_bvid": None, "fixed_url": None, "content_id_policy": "first observable feed video per independent session; BVID optional when visible; content signature required", "content_anchor_policy": "device-observed feed card plus title/duration/content signature; no fabricated or canonical fallback", "collection_design": "balanced randomized order with no-op controls; v2 and signed v3 dual-write"}))
        ui = BilibiliUi(backend, args.bundle)
        initial = ui.state(now_id("bilibili_gate2_layout"))
        if initial.get("layout_node_count", 0) < 1:
            raise SystemExit(json.dumps({"status": "blocked", "error": "Bilibili has no observable application layout", "state": initial}, ensure_ascii=False))
        print(json.dumps({"status": "ready", "target": backend.target, "permissions": permissions, "bilibili_pid": pid, "initial_state": initial, "operations": list(operation_ids), "rounds": args.rounds, "content_id_policy": "first observable feed video per independent session", "schedule_seed": args.schedule_seed, "capture_profile": capture_profile.to_metadata(), "collection_contract": COLLECTION_CONTRACT.to_metadata()}, ensure_ascii=False))
        for scheduled in schedule:
            operation_id = scheduled.operation_id
            trial_id = f"{session_id}_r{scheduled.round_index:02d}_p{scheduled.position:02d}_{operation_id.rsplit('.', 1)[-1]}"
            result = run_trial(backend, ui, database, layout, adapter, operation_id, session_id, trial_id, scheduled.round_index, capture_profile=capture_profile, schedule_position=scheduled.position)
            results.append(result)
            print(json.dumps(result, ensure_ascii=False), flush=True)
            if result["status"] != "success" and result.get("error_code") in {"HDC_DISCONNECTED", "HDC_TIMEOUT", "PROCESS_NOT_FOUND", "PROCESS_AMBIGUOUS", "PROCESS_RESTARTED", "PERMISSION_DENIED"}:
                raise RuntimeError(f"device became unavailable after {trial_id}: {result}")
        success = sum(item["status"] == "success" for item in results)
        print(json.dumps({"status": "completed", "target": backend.target, "session_id": session_id, "permissions": permissions, "trials_requested": len(schedule), "success": success, "failed": len(results) - success, "results": results}, ensure_ascii=False, indent=2))
        return 0 if success == len(results) else 2
    finally:
        backend.close()
        database.close()


if __name__ == "__main__":
    raise SystemExit(main())
