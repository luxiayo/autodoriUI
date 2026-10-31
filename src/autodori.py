import argparse
import datetime
import json
import logging
import random
import re
import string
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Optional, Union

import requests


def data_path(relative_path):
    """
    Get the path for persistent data, ensuring files are saved in the same directory as the exe after packaging, rather than in a temporary folder.
    """
    if getattr(sys, 'frozen', False):
        base_path = Path(sys.executable).parent
    else:
        base_path = Path(__file__).parent.parent

    return base_path / relative_path


config_path = data_path("config/config.yml")
"""
data_path = Path("data")
data_path.mkdir(exist_ok=True)
cache_path = Path("cache")
cache_path.mkdir(exist_ok=True)
config_path = Path("data/config.yml")
Path("debug").mkdir(exist_ok=True)
"""
if not config_path.exists():
    config_path.parent.mkdir(exist_ok=True)
    config_path.touch()
    config_path.write_text("{}", encoding="utf-8")

import numpy as np
from fuzzywuzzy import fuzz
from maa.context import Context
from maa.controller import AdbController
from maa.custom_action import CustomAction, CustomRecognitionResult
from maa.custom_recognition import CustomRecognition
from maa.define import RectType
from maa.resource import Resource
from maa.tasker import Tasker
from maa.toolkit import AdbDevice, Toolkit
from minitouchpy import (
    MNT,
    MNTEvATive7LogEventData,
    MNTEvent,
    MNTEventData,
    MNTServerCommunicateType,
)

import player
from api import BestdoriAPI
from chart import Chart, PlayRecord, TAIL_RELEASE_GRACE_MS
from util import *
import version
import cv2

# minitouchpy logs the whole command batch on every publish at INFO level
# ("send operation: ..."), i.e. several KB of formatting plus GUI log traffic
# per slice. That cost lands right at the slice boundary, where only a 3 ms
# margin is left, so keep the package quiet above WARNING.
logging.getLogger("minitouch.py").setLevel(logging.WARNING)

MIN_LIVEBOOST = 1
LIVEMODE = "freelive"
DIFFICULTY = "hard"
OFFSET = {"up": 0, "down": 0, "move": 0, "wait": 0.0, "interval": 0.0}
PHOTOGATE_LATENCY = 36
DEFAULT_MOVE_SLICE_SIZE = 10
MAX_FAILED_TIMES = 10
CMD_SLICE_SIZE = 100
MAX_CONTINUOUS_FAILED_TIMES = 10
STABLE_THRESHOLD = 3
CONSECUTIVE_FRAMES_NEEDED = 300
FREEZE_SLEEP_TIME = 0.005
CONFIDENCE_THRESHOLD_FAILURE = 0.8
CONFIDENCE_THRESHOLD_PLAY = 0.9
MAX_CONTINUOUS_NOT_FC_COUNT = 1  # A song will be skipped after failing to achieve a Full Combo this many times.
MAX_ATTEMPT_COUNT = 1  # Maximum number of attempts per song in a single task session
PLAY_COUNT_TARGET = 9  # boot_auto mode: stop the task after this many completed songs (0 = unlimited)
PLAY_COMPLETED_COUNT = 0  # boot_auto mode: songs completed in the current task session
RECOVER_COUNT = 0  # consecutive times the post-live loop had to re-enter the navigation chain
PLAY_FAILED_TIMES = 0
HUMAN_DELAY_ENABLED = False
# Verbose timing instrumentation: per-slice underrun lines and the raw minitouch
# command stream. The per-song summary (start frame, offsets, judgement) is
# always logged; this switch only adds the noisy detail, so leave it off for
# normal use and turn it on from the 全局变量调试 tab when debugging.
DEBUG_LOGGING = False
# Errnos that mean the adb/minitouch pipe died rather than a timing bug:
# 22 EINVAL (Windows write to a closed pipe), 32 EPIPE, 104 ECONNRESET,
# 232 EPIPE variant, and the Winsock equivalents.
CONNECTION_ERRNOS = {22, 32, 104, 232, 10053, 10054, 10060}
GAME_PACKAGE = "com.bilibili.star.bili"
IS_FULL_SONG = False
IS_HIGH_DIFFICULTY = False
SUPPORTED_DIFFICULTIES = ['easy', 'normal', 'hard', 'expert', 'special']
NOT_FC_SONG_COUNT_DICT: dict[str, int] = {}  # Global variable to track songs that were not Full Combo'd.
LAST_PLAYED_SONG_ID: Optional[str] = None  # Variable to record the last played song ID
SONG_ATTEMPT_COUNT_DICT: dict[str, int] = {}  # Record total attempt count per song
IS_INITIALISED = False  # Global initialisation status flag

config = yaml.safe_load(config_path.read_text(encoding="utf-8"))
maaresource = Resource()
maatasker = Tasker()
maacontroller: AdbController = None
device: AdbDevice = None
current_player: player.Player = None
current_orientation: int = 0
mnt: MNT = None
all_songs: dict = BestdoriAPI.get_song_list()
all_song_name_indexes: dict[str, str] = {
    list(filter(lambda title: title is not None, sinfo["musicTitle"]))[0]: sid
    for sid, sinfo in all_songs.items()
}
current_song_name: str = None
current_song_id: str = None
current_chart: Chart = None
play_failed_times: int = 0
callback_data: dict = {}
callback_data_lock = threading.Lock()
cmd_log_list: list = []
cmd_log_list_lock = threading.Lock()
current_version = version.VERSION
stop_event = threading.Event()
playback_started_event = threading.Event()


def reset_callback_data():
    global callback_data
    callback_data = {
        "wait": {"total": 0, "total_offset": 0.0},
        "move": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "up": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "down": {"uncommited": 0, "total": 0, "total_offset": 0.0},
        "interval": {"total": 0, "total_offset": 0.0},
        "last_cmd_endtime": -1,
    }


reset_callback_data()


def check_song_available(name, id_, difficulty):
    if name.startswith("[FULL]") and not IS_FULL_SONG:
        return False

    # boot_auto plays a fixed number of songs before stopping, so the free-auto
    # strategy limits must not apply: blacklisting a song after N attempts (or
    # after a non-FC run) would shrink the 收藏2 pool until nothing is left.
    if id_ and LIVEMODE != "boot_auto":
        attempt_count = SONG_ATTEMPT_COUNT_DICT.get(id_, 0)
        if attempt_count >= MAX_ATTEMPT_COUNT:
            logging.warning(f"Song '{name}' has reached max attempt count ({attempt_count}).")
            return False

        not_fc_count = NOT_FC_SONG_COUNT_DICT.get(id_, 0)
        if not_fc_count >= MAX_CONTINUOUS_NOT_FC_COUNT:
            logging.warning(f"Song '{name}' has reached the non-FC limit ({not_fc_count}).")
            NOT_FC_SONG_COUNT_DICT[id_] = 0
            return False

    """
    lastmatched = PlayRecord.get_or_none(chart_id=id_, difficulty=difficulty)
    if lastmatched:
        if not lastmatched.succeed:
            return True
    """

    return True


@maaresource.custom_recognition("SongRecognition")
class SongRecognition(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:

        if LIVEMODE == "medley_single":
            roi = [110, 545, 370, 30]
        elif LIVEMODE == "free_single":
            roi = [220, 545, 570, 30]
        elif LIVEMODE in ["free_auto", "boot_auto"]:
            # Both modes read the title on the 自由演出 song-select screen.
            roi = [200, 330, 370, 30]

        models_to_try = ["ppocr_v5/zh_cn-server",""]

        def match(model=None):
            pipeline = {
                "_ocr_song": {
                    "recognition": "OCR",
                    "only_rec": True,
                    "roi": roi,
                },
            }
            if model != None:
                pipeline["_ocr_song"]["model"] = model
            try:
                ocr_text = context.run_recognition("_ocr_song", argv.image, pipeline).best_result.text
                logging.info(f"OCR ({model or 'default'}) raw text: '{ocr_text}'")

                if not ocr_text:
                    return None

                if LIVEMODE not in ["medley_single", "free_single"] and "full" in ocr_text.lower():
                    return 100

                match = fuzzy_match_song(ocr_text)
                if not match:
                    return None

                matched_name, adjusted_score, raw_score = match

                len_ocr = len(ocr_text)
                len_matched = len(matched_name)
                length_ratio = min(len_ocr, len_matched) / max(len_ocr, len_matched) if len_ocr and len_matched else 0

                logging.info(
                    f"Adjusted score for '{matched_name}' with length penalty ({model or 'default'}): "
                    f"{adjusted_score:.2f} (raw: {raw_score}, len_ratio: {length_ratio:.2f})"
                )
                return (matched_name, adjusted_score)

            except Exception as e:
                logging.error(f"OCR ({model or 'default'}) execution failed: {e}")
                return None

        results = [m for m in [match(model) for model in models_to_try] if m]

        if not results or 100 in results:
            return self.AnalyzeResult(None, "")

        best_match = max(results, key=lambda x: x[1])

        if best_match and best_match[1] > 50:
            matched_song_name = best_match[0]
            adjusted_confidence = best_match[1]

            if LIVEMODE == "free_single":
                if IS_FULL_SONG:
                    matched_song_name = "[FULL] " + matched_song_name
                elif IS_HIGH_DIFFICULTY:
                    matched_song_name = "[瓒呴珮闆ｆ槗搴?SPECIAL] " + matched_song_name

            song_id = all_song_name_indexes.get(best_match[0])

            if not check_song_available(matched_song_name, song_id, DIFFICULTY):
                return self.AnalyzeResult(None, "")

            logging.info(f"Song recognised: '{matched_song_name}' (Adjusted Confidence: {adjusted_confidence:.2f}%)")
            return self.AnalyzeResult(roi, matched_song_name)

        return self.AnalyzeResult(None, "")



@maaresource.custom_recognition("LiveBoostEnoughRecognition")
class LiveBoostEnoughRecognition(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:
        # roi = [970, 29, 39, 21]
        roi = [979, 30, 61, 20]

        pipeline = {
            "live_boost_enough_ocr": {
                "recognition": "OCR",
                "only_rec": True,
                "roi": roi,
            },
        }
        live_boost = context.run_recognition(
            "live_boost_enough_ocr",
            argv.image,
            pipeline,
        ).best_result.text

        logging.debug("Live boost rec result: {}".format(live_boost))
        pattern = r"^\s*(\d+)\s*/"
        match = re.match(pattern, live_boost.replace(" ", ""))

        if match:
            try:
                live_boost = int(match.group(1))
            except:
                live_boost = -1
        else:
            live_boost = -1

        logging.debug("Live boost: {}".format(live_boost))
        return CustomRecognition.AnalyzeResult(roi, str(live_boost))


@maaresource.custom_action("HandleLiveBoost")
class HandleLiveBoost(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        liveboost = int(argv.reco_detail.best_result.detail)
        if liveboost < MIN_LIVEBOOST:
            logging.debug("Live boost not enough, ready to exit")
            context.run_action("close_app")
            context.run_action("stop")
        return CustomAction.RunResult(True)


@maaresource.custom_recognition("PlayResultRecognition")
class PlayResultRecognition(CustomRecognition):
    def analyze(
        self, context: Context, argv: CustomRecognition.AnalyzeArg
    ) -> Union[CustomRecognition.AnalyzeResult, Optional[RectType]]:

        types = {
            "score": {
                "roi": [1028, 192, 144, 35],
            },
            "maxcombo": {
                "roi": [1009, 391, 91, 28],
            },
            "perfect": {
                "roi": [829, 282, 90, 28],
            },
            "great": {
                "roi": [828, 322, 91, 27],
            },
            "good": {
                "roi": [829, 363, 91, 27],
            },
            "bad": {
                "roi": [829, 401, 90, 27],
            },
            "miss": {
                "roi": [830, 438, 91, 28],
            },
            "fast": {
                "roi": [1088, 283, 90, 27],
            },
            "slow": {
                "roi": [1088, 323, 91, 28],
            },
        }
        result = {type_: {} for type_ in types.keys()}
        pipeline = {
            f"_PlayResultRecognition_ocr_{type_}": {
                "recognition": "OCR",
                "only_rec": True,
                "roi": type_value["roi"],
            }
            for type_, type_value in types.items()
        }
        for type_, _ in types.items():
            try:
                ocrtext = context.run_recognition(
                    f"_PlayResultRecognition_ocr_{type_}",
                    argv.image,
                    pipeline,
                ).best_result.text
                type_result = int(ocrtext)
            except:
                type_result = -1
            result[type_] = type_result

        logging.debug("Play result: {}".format(result))
        return CustomRecognition.AnalyzeResult([0, 0, 0, 0], json.dumps(result))


@maaresource.custom_action("SavePlayResult")
class SavePlayResult(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        try:
            global PLAY_FAILED_TIMES, NOT_FC_SONG_COUNT_DICT, LAST_PLAYED_SONG_ID, SONG_ATTEMPT_COUNT_DICT
            global PLAY_COMPLETED_COUNT, RECOVER_COUNT

            param = argv.custom_action_param
            try:
                succeed = json.loads(param).get("succeed", False)
            except (json.JSONDecodeError, TypeError):
                logging.error("Failed to parse custom_action_param.")
                succeed = False

            playresult = {}
            if succeed and argv.reco_detail and argv.reco_detail.best_result:
                try:
                    playresult = argv.reco_detail.best_result.detail

                    perfect = playresult.get('perfect', -1)
                    great = playresult.get('great', -1)
                    good = playresult.get('good', -1)
                    bad = playresult.get('bad', -1)
                    miss = playresult.get('miss', -1)
                    maxcombo = playresult.get('maxcombo', -1)
                    fast = playresult.get('fast', -1)
                    slow = playresult.get('slow', -1)

                    logging.info(
                        f"Judgement detail: perfect={perfect} great={great} good={good} "
                        f"bad={bad} miss={miss} maxcombo={maxcombo} fast={fast} slow={slow}"
                    )
                    # FAST/SLOW is the game's own verdict on whether hits landed
                    # early or late: the only ground truth for the start phase.
                    if fast >= 0 and slow >= 0 and (fast + slow) > 0:
                        bias = fast - slow
                        if bias > 0:
                            logging.info(
                                f"Timing bias: fast-slow={bias} (hits tend to be EARLY) "
                                f"-> raise PHOTOGATE_LATENCY above {PHOTOGATE_LATENCY}"
                            )
                        elif bias < 0:
                            logging.info(
                                f"Timing bias: fast-slow={bias} (hits tend to be LATE) "
                                f"-> lower PHOTOGATE_LATENCY below {PHOTOGATE_LATENCY}"
                            )
                        else:
                            logging.info("Timing bias: fast-slow=0 (balanced)")

                    is_not_fc = False
                    reasons = []

                    is_ap_by_sum = (
                            perfect != -1 and
                            great != -1 and
                            maxcombo != -1 and
                            (perfect + great) == maxcombo
                    )

                    if is_ap_by_sum:
                        is_not_fc = False
                    else:
                        if -1 in [perfect, great, good, bad, miss, maxcombo]:
                            # A single unreadable number must not turn a clean run
                            # into a failure. When GOOD/BAD/MISS all read as 0 and
                            # there is at least one PERFECT, the run is an All
                            # Perfect even if GREAT (0 in that case) failed OCR.
                            clean = (
                                perfect > 0
                                and good == 0
                                and bad == 0
                                and miss == 0
                            )
                            if clean:
                                logging.info(
                                    "Judgement OCR incomplete (great/maxcombo unreadable), "
                                    "but GOOD/BAD/MISS are all 0 -> treating as All Perfect."
                                )
                            else:
                                is_not_fc = True
                                reasons.append("OCR Failed")
                        else:
                            if bad > 0:
                                reasons.append(f"Bad: {bad}")
                            if miss > 0:
                                reasons.append(f"Miss: {miss}")
                            if good > 0:
                                reasons.append(f"Good: {good}")

                            sum_of_judgements = perfect + great
                            if maxcombo != sum_of_judgements:
                                reasons.append(f"P+G={sum_of_judgements}, MaxCombo={maxcombo}")

                            if reasons:
                                is_not_fc = True

                    if is_not_fc and current_song_id:
                        current_count = NOT_FC_SONG_COUNT_DICT.get(current_song_id, 0)
                        NOT_FC_SONG_COUNT_DICT[current_song_id] = current_count + 1
                        logging.warning(
                            f"Song '{current_song_name}' did not achieve a Full Combo. "
                            f"Total count: {NOT_FC_SONG_COUNT_DICT[current_song_id]} "
                            f"Reasons: {', '.join(reasons)}"
                        )

                except json.JSONDecodeError:
                    logging.error("Failed to parse play result JSON.")
                    playresult = {}

            # Increment failure count only on explicit failures (e.g., live failed, pipeline error).
            if succeed:
                # If task is successful and there were previous failures, log and reset counter
                if PLAY_FAILED_TIMES > 0:
                    logging.info(
                        f"Task successful, resetting continuous failure count from {PLAY_FAILED_TIMES} to zero.")
                PLAY_FAILED_TIMES = 0
                PLAY_COMPLETED_COUNT += 1
                # A finished song means the loop is healthy again.
                RECOVER_COUNT = 0
                if PLAY_COUNT_TARGET > 0:
                    logging.info(f"Completed songs: {PLAY_COMPLETED_COUNT}/{PLAY_COUNT_TARGET}")
            else:  # 'not succeed' case
                # If task failed, increment counter unconditionally
                PLAY_FAILED_TIMES += 1
                logging.info(f"Recording one task failure, current continuous failure count: {PLAY_FAILED_TIMES}")

            PlayRecord.create(
                play_time=int(time.time()),
                play_offset=OFFSET,
                result=playresult,
                succeed=succeed,
                chart_id=current_song_id,
                difficulty=DIFFICULTY,
            )

            if current_song_id:
                current_attempts = SONG_ATTEMPT_COUNT_DICT.get(current_song_id, 0)
                SONG_ATTEMPT_COUNT_DICT[current_song_id] = current_attempts + 1
                logging.info(
                    f"Song '{current_song_name}' has been attempted {SONG_ATTEMPT_COUNT_DICT[current_song_id]} times.")

            LAST_PLAYED_SONG_ID = current_song_id

            if PLAY_COUNT_TARGET > 0 and PLAY_COMPLETED_COUNT >= PLAY_COUNT_TARGET:
                logging.info(
                    f"Reached the target of {PLAY_COUNT_TARGET} completed songs "
                    f"({PLAY_COMPLETED_COUNT} done). Stopping the task."
                )
                # Leave the game running: the user asked to stay in-game after
                # a finished session, so only the task stops.
                context.run_action("stop")
                return CustomAction.RunResult(True)

            if PLAY_FAILED_TIMES >= MAX_CONTINUOUS_FAILED_TIMES:
                logging.error(
                    f"Continuous failure limit reached ({MAX_CONTINUOUS_FAILED_TIMES}). Stopping automatically.")
                context.run_action("close_app")
                context.run_action("stop")
            return CustomAction.RunResult(True)
        except Exception as e:
            logging.error(f"Failed to save play result: {e}")
            return CustomAction.RunResult(False)


@maaresource.custom_action("Play")
class Play(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        global stop_event, playback_started_event
        stop_event.clear()
        playback_started_event.clear()
        monitor = threading.Thread(
            target=monitor_failure_thread,
            args=(stop_event, playback_started_event),
            daemon=True
        )
        try:
            monitor.start()
            play_song(stop_event, playback_started_event)
            stop_event.set()
            return CustomAction.RunResult(True)
        except Exception as e:
            stop_event.set()
            logging.error(f"Error during song playback: {e}", exc_info=True)
            if is_connection_error(e):
                # The emulator went away mid-song: that is a device problem,
                # not a timing problem, so say so and rebuild the pipe.
                logging.warning(
                    "The device connection dropped during playback (emulator crash or restart); "
                    "rebuilding the touch session and resuming from the menus."
                )
                rebuild_touch_session()
            return CustomAction.RunResult(False)
        finally:
            monitor.join(timeout=5)


@maaresource.custom_action("SaveSong")
class SaveSong(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        name: CustomRecognitionResult = argv.reco_detail.best_result.detail
        save_song(name)
        return CustomAction.RunResult(True)


@maaresource.custom_action("RecoverAfterSong")
class RecoverAfterSong(CustomAction):
    """
    Fallback for when the post-live 再来一次 button never appears.

    A live sometimes drops straight back to the main page instead of showing the
    result screen (for example when a story scene follows it), so liveagain never
    matches and the loop used to give up and stop the whole session. Re-enter the
    navigation chain instead, giving up only after several tries in a row so a
    genuinely stuck game cannot loop forever.
    """

    MAX_RECOVERIES = 5

    def run(self, context: Context, argv: CustomAction.RunArg):
        global RECOVER_COUNT
        RECOVER_COUNT += 1
        logging.warning(
            f"再来一次 was not found; re-entering the navigation chain "
            f"({RECOVER_COUNT}/{self.MAX_RECOVERIES})."
        )
        if RECOVER_COUNT >= self.MAX_RECOVERIES:
            logging.error("Too many recoveries without finishing a song; stopping the task.")
            RECOVER_COUNT = 0
            context.run_action("stop")
            return CustomAction.RunResult(True)
        # The reason 再来一次 was missing may be that the emulator went away
        # mid-song; make sure the capture/touch stack matches a live VM before
        # walking the menus again.
        try:
            revalidate_emulator_stack()
        except Exception as e:
            logging.error(f"Emulator is still unusable after the failure, stopping: {e}")
            RECOVER_COUNT = 0
            context.run_action("stop")
        return CustomAction.RunResult(True)


@maaresource.custom_action("EnsureFav2Filter")
class EnsureFav2Filter(CustomAction):
    """
    Make the song-select screen use the 收藏2 filter.

    The left sidebar is a scrollable list of categories: 收藏2 is only visible
    once the 收藏 group is expanded, and 收藏 itself can sit below the fold
    (3D演出 / 原创歌曲 / 虚拟歌手 / J-POP ... push it out of view). A fixed
    pipeline chain cannot express "look, maybe scroll, look again", and doing
    it with error-handling jumps trips MAA's loop detection, so it lives here.
    """

    SIDEBAR_ROI = [0, 100, 200, 620]
    MAX_ATTEMPTS = 4

    # The rows carry a star glyph once they are the active filter: OCR returns
    # "\U0001f31f\u6536\u85cf2", so an exact match on "\u6536\u85cf2" never fires. Keep the
    # patterns loose on both sides instead.
    FAV2_PATTERN = ".*\u6536\u85cf2.*"
    FAV_PATTERN = ".*\u6536\u85cf$"

    @staticmethod
    def _grab_image(fallback=None):
        try:
            return maacontroller.post_screencap().wait().get()
        except Exception as e:
            logging.debug(f"EnsureFav2Filter: screencap failed: {e}")
            return fallback

    @staticmethod
    def _find(context, image, pattern):
        if image is None:
            return None
        pipeline = {
            "_fav_ocr": {
                "recognition": "OCR",
                "only_rec": False,
                "roi": EnsureFav2Filter.SIDEBAR_ROI,
                "expected": [pattern],
            }
        }
        try:
            res = context.run_recognition("_fav_ocr", image, pipeline)
        except Exception as e:
            logging.debug(f"EnsureFav2Filter: OCR for {pattern!r} failed: {e}")
            return None
        return res.best_result if res and res.best_result else None

    @staticmethod
    def _click(box):
        x, y, w, h = box
        maacontroller.post_click(int(x + w / 2), int(y + h / 2)).wait()

    def run(self, context: Context, argv: CustomAction.RunArg):
        # CustomAction.RunArg carries the recognition detail but no screenshot,
        # so take one explicitly before looking at the sidebar.
        image = self._grab_image()

        for attempt in range(1, self.MAX_ATTEMPTS + 1):
            hit = self._find(context, image, "收藏2")
            if hit:
                self._click(hit.box)
                logging.info(f"Song filter: 收藏2 selected (attempt {attempt}).")
                time.sleep(1.0)
                return CustomAction.RunResult(True)

            hit = self._find(context, image, "^收藏$")
            if hit:
                self._click(hit.box)
                logging.info(f"Song filter: expanded the 收藏 group (attempt {attempt}).")
                time.sleep(1.5)
                image = self._grab_image(image)
                hit = self._find(context, image, "收藏2")
                if hit:
                    self._click(hit.box)
                    logging.info("Song filter: 收藏2 selected after expanding.")
                    time.sleep(1.0)
                    return CustomAction.RunResult(True)

            logging.info(f"Song filter: 收藏2 not visible, scrolling the sidebar (attempt {attempt}).")
            maacontroller.post_swipe(90, 620, 90, 260, 400).wait()
            time.sleep(1.0)
            image = self._grab_image(image)

        logging.warning("Song filter: could not select 收藏2, keeping the current filter.")
        return CustomAction.RunResult(True)


def _similarity(a: str, b: str) -> int:
    """Case-insensitive, Unicode-safe similarity (keeps CJK titles intact)."""
    return fuzz.ratio(a.lower(), b.lower())


def fuzzy_match_song(name):
    """
    Rank every song title by *length-adjusted* similarity and return the best
    match as (matched_name, adjusted_score, raw_score), or None.

    The length adjustment must happen before the winner is picked. With
    fuzzywuzzy's default WRatio, a short OCR fragment scores ~90 against a long
    title through partial matching ('XIST' vs 'Believe in my existence' = 90),
    which barely beats the correct short title ('XIST' vs 'EXIST' = 89). Picking
    the raw winner first and shortening the score afterwards therefore throws
    away the correct answer and only ever ends in a rejection.
    """
    if not name:
        return None

    best = None
    for candidate in all_song_name_indexes:
        if not candidate:
            continue

        raw_score = _similarity(name, candidate)
        if not raw_score:
            continue

        length_ratio = min(len(name), len(candidate)) / max(len(name), len(candidate))
        adjusted_score = raw_score * length_ratio

        if best is None or adjusted_score > best[1]:
            best = (candidate, adjusted_score, raw_score)

    return best


def _get_orientation(max_attempts: int = 3):
    """
    0, 1, 2, 3
    0: 0°
    1: 90°
    2: 180°
    3: 270°

    The value decides how android coordinates are rotated onto minitouch's own
    (portrait) coordinate space. Falling back to 0 silently would send every
    touch to the wrong place, so a failure is retried and then raised instead.
    """
    command_list = [
        str(device.adb_path.absolute()),
        "-s",
        device.address,
        "shell",
        "dumpsys input|grep SurfaceOrientation",
    ]
    logging.debug("get SurfaceOrientation command: {}".format(" ".join(command_list)))

    for attempt in range(1, max_attempts + 1):
        try:
            output = subprocess.check_output(
                command_list, encoding="utf-8", errors="replace", timeout=10
            )
            match = re.search(r"SurfaceOrientation:\s*(\d+)", output)
            if match is None:
                raise RuntimeError(f"pattern not found in output: {output[:200]!r}")
            orientation = int(match.group(1))
            logging.debug("SurfaceOrientation: {}".format(orientation))
            return orientation
        except Exception as e:
            logging.warning(
                f"Failed to get SurfaceOrientation (attempt {attempt}/{max_attempts}): {e}"
            )
            time.sleep(0.5 * attempt)

    raise RuntimeError(
        "Cannot determine device orientation; refusing to play with wrong touch coordinates."
    )


def save_song(name):
    global current_song_name, current_song_id, current_chart, current_orientation
    current_song_name = name
    current_song_id = all_song_name_indexes[current_song_name]
    current_chart = Chart((current_song_id, DIFFICULTY), current_song_name)
    current_chart.notes_to_actions(current_player.resolution, DEFAULT_MOVE_SLICE_SIZE, humanize=HUMAN_DELAY_ENABLED)
    current_orientation = _get_orientation()
    current_chart.actions_to_MNTcmd(
        (mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE
    )
    logging.info(f"Saved song: {name}")


def play_song(stop_event, playback_started_event):

    logging.info("Start play")
    logging.info(
        f"Playback settings: song='{current_song_name}' difficulty={DIFFICULTY} "
        f"photogate={PHOTOGATE_LATENCY}ms tail_grace={TAIL_RELEASE_GRACE_MS}ms "
        f"slice={CMD_SLICE_SIZE} move_slice={DEFAULT_MOVE_SLICE_SIZE} "
        f"humanize={HUMAN_DELAY_ENABLED} debug_log={DEBUG_LOGGING}"
    )
    cmd_log_list.clear()
    reset_callback_data()

    def _get_wait_time():
        wait_for = 0.0
        index = current_chart.actions_to_cmd_index
        for action in current_chart.actions[index - CMD_SLICE_SIZE : index]:
            if action["type"] == "wait":
                wait_for += action["length"]
        return wait_for

    def _adjust_offset():
        global callback_data
        total_cost = 0.0
        for type_ in ["up", "down", "move", "wait", "interval"]:
            type_data = callback_data[type_]
            total = type_data["total"]
            if total != 0:
                total_cost += type_data["total_offset"] - OFFSET[type_] * total
                OFFSET[type_] = type_data["total_offset"] / total

        current_chart._a2c_offset += total_cost
        logging.debug("Adjust offset: {}".format(OFFSET))
        logging.debug("Adjust _actions_to_cmd_offset: {}".format(total_cost))

    logging.info("Waiting for game to load, detecting pause button.")
    template_path = data_path("assets/resource/image/live/button/pause.png")
    if not template_path.exists():
        logging.error(f"Pause button template image not found: {template_path}")
        return
    template = get_scaled_template(template_path)
    if template is None:
        logging.error(f"Failed to load template image: {template_path}")
        return

    pause_button_found = False
    wait_start_time = time.time()
    playback_started_event.set()

    while not pause_button_found:
        wait_timeout = 30
        wait_current_time = time.time()
        if wait_current_time - wait_start_time > wait_timeout:
            logging.error(f"Waiting for pause button timeout ({wait_current_time - wait_start_time}s), aborting.")
            return
        if check_exit_status(stop_event):
            return

        screen = current_player.ipc_capture_display()
        height, width, _ = screen.shape
        roi_screen = screen[0:int(height * 0.15), width - int(height * 0.15):width]
        gray_roi = cv2.cvtColor(roi_screen, cv2.COLOR_BGR2GRAY)
        result = cv2.matchTemplate(gray_roi, template, cv2.TM_CCOEFF_NORMED)
        _, max_val, _, _ = cv2.minMaxLoc(result)

        logging.debug(f"Waiting for pause button, match confidence: {max_val:.2f}")
        if max_val >= CONFIDENCE_THRESHOLD_PLAY:
            logging.info("Pause button detected, waiting for screen frozen.")
            pause_button_found = True
        else:
            time.sleep(0.5)

    if not wait_first_note(stop_event):
        return

    slice_count = 0
    max_underrun_ms = 0.0
    total_underrun_ms = 0.0
    max_publish_ms = 0.0

    while True:
        if check_exit_status(stop_event):
            return

        publish_start = time.perf_counter()
        current_chart.command_builder.publish(mnt, block=False)
        max_publish_ms = max(max_publish_ms, (time.perf_counter() - publish_start) * 1000)

        wait_time = _get_wait_time()
        requested_sleep = max(0, wait_time - 3) / 1000

        sleep_start = time.perf_counter()
        time.sleep(requested_sleep)
        sleep_ms = (time.perf_counter() - sleep_start) * 1000

        build_start = time.perf_counter()
        index = current_chart.actions_to_cmd_index
        has_more = bool(current_chart.actions[index : index + CMD_SLICE_SIZE])
        if has_more:
            with callback_data_lock:
                _adjust_offset()
                reset_callback_data()
            current_chart.actions_to_MNTcmd(
                (mnt.max_x, mnt.max_y), current_orientation, OFFSET, CMD_SLICE_SIZE
            )
        build_ms = (time.perf_counter() - build_start) * 1000

        # Time the device sat without new commands: sleep overshoot plus the
        # time spent compiling the next slice, minus the 3 ms margin the loop
        # deliberately keeps. Positive values mean the queue ran dry.
        underrun_ms = (sleep_ms - requested_sleep * 1000) + build_ms - 3
        max_underrun_ms = max(max_underrun_ms, underrun_ms)
        total_underrun_ms += underrun_ms
        slice_count += 1

        if DEBUG_LOGGING:
            offsets = " ".join(f"{k}={v:.2f}" for k, v in OFFSET.items())
            logging.info(
                f"slice {slice_count}: underrun={underrun_ms:+.1f}ms build={build_ms:.1f}ms "
                f"publish={max_publish_ms:.1f}ms offsets[{offsets}]"
            )

        if not has_more:
            break

    logging.info(
        f"Playback stats: slices={slice_count} "
        f"max_underrun={max_underrun_ms:.1f}ms "
        f"avg_underrun={total_underrun_ms / max(1, slice_count):.1f}ms "
        f"max_publish={max_publish_ms:.1f}ms"
    )
    logging.info(
        "Final offsets: " + ", ".join(f"{k}={v:.2f}" for k, v in OFFSET.items())
    )

    time.sleep(2)
    logging.info("Playback finished.")


def wait_first_note(stop_event):
    last_color = None
    waited_frames = 0
    info = get_runtime_info(current_player.resolution)["wait_first"]
    from_row, to_row = info["from"], info["to"]
    freezed = False
    playback_start_time = time.time()

    frame_gaps_ms = []
    last_frame_ts = None
    frames_after_freeze = 0
    pre_trigger_max_change = 0.0
    pre_trigger_frames = 0

    while True:
        playback_timeout = 500
        playback_current_time = time.time()
        if playback_current_time - playback_start_time > playback_timeout:
            logging.error(f"Playback timeout ({playback_current_time - playback_start_time}s), aborting.")
            return False
        if check_exit_status(stop_event):
            return False

        try:
            screen = current_player.ipc_capture_display()
            cur_color, _ = get_color_eval_in_range(screen, from_row, to_row)

            now = time.perf_counter()
            if last_frame_ts is not None:
                frame_gaps_ms.append((now - last_frame_ts) * 1000)
            last_frame_ts = now
            median_gap = float(np.median(frame_gaps_ms)) if frame_gaps_ms else 0.0

            if last_color is not None:
                change_score = np.sum(np.abs(cur_color[:3].astype(int) - last_color[:3].astype(int)))

                if change_score > STABLE_THRESHOLD:
                    if freezed:
                        # This single frame decides the phase of the whole song:
                        # a mis-timed trigger shifts every note by one frame.
                        logging.info(
                            f"First note detected after {frames_after_freeze} frame(s) "
                            f"(median frame interval {median_gap:.1f}ms, "
                            f"change={change_score:.1f}); starting playback in {PHOTOGATE_LATENCY}ms."
                        )
                        # The band is watched for seconds before the first note.
                        # If this noise floor creeps close to STABLE_THRESHOLD, a
                        # stray effect can start the whole song far too early.
                        logging.info(
                            f"Pre-trigger band noise: max_change={pre_trigger_max_change:.1f} "
                            f"over {pre_trigger_frames} frame(s), threshold={STABLE_THRESHOLD}"
                        )
                        time.sleep(PHOTOGATE_LATENCY / 1000)
                        return True
                else:
                    if not freezed:
                        waited_frames += 1
                    else:
                        frames_after_freeze += 1
                        pre_trigger_max_change = max(pre_trigger_max_change, change_score)
                        pre_trigger_frames += 1

                if not freezed and waited_frames >= CONSECUTIVE_FRAMES_NEEDED:
                    freezed = True
                    logging.info(
                        f"Screen has frozen after {waited_frames} frames "
                        f"(~{waited_frames * median_gap / 1000:.2f}s, median frame interval {median_gap:.1f}ms). "
                        "Photogate is ready."
                    )

            last_color = cur_color
            time.sleep(FREEZE_SLEEP_TIME)

        except Exception as e:
            logging.error(f"Error during photogate detection: {e}")
            return False


def check_exit_status(stop_event):
    if stop_event.is_set():
        logging.warning("Playback failed, exiting.")
        return True
    return False


def init_maa():
    res_job = maaresource.post_bundle(data_path("assets/resource"))
    res_job.wait()
    Toolkit.init_option(data_path(""))
    for i in range(3):
        adb_devices = Toolkit.find_adb_devices()
        if adb_devices:
            break
    if not adb_devices:
        raise RuntimeError("No ADB devices found.")

    global device, maacontroller
    _device: list[AdbDevice] = []
    for device in adb_devices:
        extra_names = device.config.get("extras", {}).keys()
        if "mumu" in extra_names or "ld" in extra_names:
            if (device.name, device.address) not in [
                (d.name, d.address) for d in _device
            ]:
                _device.append(device)

    """
    filter_str = config.get("device", {}).get("filter", "devices")
    _device = eval(filter_str, {}, {"devices": _device})
    """

    if not _device:
        raise RuntimeError("No supported emulators found.")
    else:
        device = _device[0]

    """
    elif len(_device) == 1:
        device = _device[0]
    elif len(_device) > 1:
        print("Multiple devices were found:")
        for i, d in enumerate(_device):
            print(f"{i}: {d.name}({d.address})")
        selected = input("Select a device: ")
        device = _device[int(selected)]
    """

    logging.info(f"Using device: {device.name} at {device.address}")

    maacontroller = AdbController(
        adb_path=device.adb_path,
        address=device.address,
        config=device.config,
    )
    """
        screencap_methods=device.screencap_methods,
        input_methods=device.input_methods,
    """

    # A freshly started emulator accepts the adb entry seconds before adbd
    # answers, so give the controller a real window instead of 3 instant tries.
    is_connected = False
    for attempt in range(1, 13):
        if maacontroller.post_connection().wait().succeeded:
            is_connected = True
            break
        if stop_event.is_set():
            raise RuntimeError("Cancelled while connecting to the emulator.")
        logging.info(f"Connecting to the controller failed, retrying ({attempt}/12)...")
        time.sleep(5)

    if not is_connected:
        raise RuntimeError(f"Failed to connect controller to device {device.name}.")

    # tasker = Tasker(notification_handler=MyNotificationHandler())
    maatasker.bind(maaresource, maacontroller)

    if not maatasker.inited:
        raise RuntimeError("Failed to initialise MAA tasker module.")

    logging.info("MAA initialised successfully.")


def mnt_callback(event: MNTEvent, data: MNTEventData):
    global callback_data
    if event == MNTEvent.EVATIVE7_LOG:
        data: MNTEvATive7LogEventData = data

        cmd = data.cmd
        cost = data.cost

        with cmd_log_list_lock:
            cmd_log_list.append(data)
        cmd_type = cmd.split(" ")[0]

        with callback_data_lock:
            if (last_cmd_endtime := callback_data.get("last_cmd_endtime")) != -1:
                callback_data["interval"]["total"] += 1
                callback_data["interval"]["total_offset"] += (
                        data.start_time - last_cmd_endtime
                )
            callback_data["last_cmd_endtime"] = data.end_time
            if cmd_type in ["w"]:
                callback_data["wait"]["total"] += 1
                callback_data["wait"]["total_offset"] += cost - int(cmd.split(" ")[-1])
            elif cmd_type in ["u", "d", "m"]:
                type_ = {
                    "u": "up",
                    "d": "down",
                    "m": "move",
                }[cmd_type]
                callback_data[type_]["uncommited"] += 1
                callback_data[type_]["total"] += 1
                callback_data[type_]["total_offset"] += cost
            elif cmd_type in ["c"]:
                total_uncommited = 0
                for type_ in ["up", "down", "move"]:
                    total_uncommited += callback_data[type_]["uncommited"]

                if total_uncommited != 0:
                    for type_ in ["up", "down", "move"]:
                        callback_data[type_]["total_offset"] += cost * (
                                callback_data[type_]["uncommited"] / total_uncommited
                        )
                        callback_data[type_]["uncommited"] = 0


def init_player_and_mnt():
    global current_player, mnt

    if not device: raise RuntimeError("MAA device not initialised before initialising player.")

    extra_config = device.config["extras"]
    if "mumu" in extra_config.keys():
        extra_config = extra_config["mumu"]
        type_ = "mumu"
        if device.name == "MuMuPlayer12":
            type_ += "v4"
        if device.name == "MuMuPlayer12 v5":
            type_ += "v5"
    elif "ld" in extra_config.keys():
        extra_config = extra_config["ld"]
        type_ = "ld"
    else:
        raise RuntimeError(f"Unsupported emulator type: {list(extra_config.keys())}")

    path = extra_config["path"]
    index = extra_config["index"]

    current_player = player.Player(type_, Path(path), index)
    mnt = MNT(
        device.address,
        type_="EvATive7",
        communicate_type=MNTServerCommunicateType.STDIO,
        mnt_asset_path=data_path("assets/minitouch_EvATive7"),
        callback=mnt_callback,
        adb_executor=str(device.adb_path.absolute()),
    )

    logging.info(f"{type_} player and Minitouch initialised successfully.")


def get_scaled_template(template_path):
    template = cv2.imread(template_path, 0)
    runtime_h, runtime_w, _ = current_player.ipc_capture_display().shape
    scale_factor = runtime_w / 1920
    if np.isclose(scale_factor, 1.0):
        return template
    original_h, original_w = template.shape[:2]
    new_w = int(original_w * scale_factor)
    new_h = int(original_h * scale_factor)
    if new_w < 1 or new_h < 1:
        return template
    resized_template = cv2.resize(template, (new_w, new_h), interpolation=cv2.INTER_AREA)
    return resized_template


def monitor_failure_thread(stop_event, playback_started_event):
    """
    A background monitoring thread.
    It waits for the playback start signal, then continuously monitors for the "Live Failed" screen through image matching.
    """
    try:
        logging.info("Monitor thread started, waiting for playback start signal.")

        # Wait for "playback started" signal from play_song function, timeout after 60s
        playback_started_event.wait(timeout=30)

        if not playback_started_event.is_set():
            logging.warning("Timeout waiting for playback start signal, monitor thread exiting.")
            stop_event.set()
            return

        logging.info("Received playback start signal, starting screen monitoring.")

        # Load template image once for efficiency
        # Note: Please ensure this path matches your project resource path
        fail_template_path = data_path("assets/resource/image/live/live_failed.png")
        if not fail_template_path.exists():
            logging.error(
                f"Live Failed template image not found: {fail_template_path}, monitor thread cannot work.")
            stop_event.set()
            return

        template = get_scaled_template(fail_template_path)

        # Monitor loop until stop signal received
        while not stop_event.is_set():
            screen_bgr = current_player.ipc_capture_display()
            if screen_bgr is None:
                time.sleep(1)
                continue

            screen_gray = cv2.cvtColor(screen_bgr, cv2.COLOR_BGR2GRAY)

            # Perform template matching
            result = cv2.matchTemplate(screen_gray, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, _ = cv2.minMaxLoc(result)

            if max_val >= CONFIDENCE_THRESHOLD_FAILURE:
                logging.error(f"Detected 'Live Failed' screen (match: {max_val:.2f})! Sending stop signal!")
                stop_event.set()  # Key: Set stop event to notify other threads
                break  # Task complete, exit loop

            # Monitor every 1s to avoid high CPU usage
            time.sleep(1)

    except Exception as e:
        logging.error(f"Monitor thread encountered unexpected error: {e}", exc_info=True)
        stop_event.set()
    finally:
        logging.info("Monitor thread terminated.")


MUMU_MANAGER_RELATIVE_PATH = Path("nx_main") / "MuMuManager.exe"


def _find_mumu_install_dir():
    """Locate MuMu Player's installation directory through the Windows registry."""
    if sys.platform != "win32":
        return None
    try:
        import winreg
    except ImportError:
        return None

    uninstall_roots = (
        r"SOFTWARE\Microsoft\Windows\CurrentVersion\Uninstall",
        r"SOFTWARE\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall",
    )
    for hive in (winreg.HKEY_LOCAL_MACHINE, winreg.HKEY_CURRENT_USER):
        for root in uninstall_roots:
            try:
                with winreg.OpenKey(hive, root) as key:
                    for index in range(winreg.QueryInfoKey(key)[0]):
                        try:
                            with winreg.OpenKey(key, winreg.EnumKey(key, index)) as entry:
                                try:
                                    display_name = str(winreg.QueryValueEx(entry, "DisplayName")[0])
                                except OSError:
                                    continue
                                if "MuMu" not in display_name:
                                    continue
                                try:
                                    location = winreg.QueryValueEx(entry, "InstallLocation")[0]
                                except OSError:
                                    continue
                                candidate = Path(location)
                                if location and (candidate / MUMU_MANAGER_RELATIVE_PATH).exists():
                                    return candidate
                        except OSError:
                            continue
            except OSError:
                continue
    return None


def _first_adb_device():
    try:
        devices = Toolkit.find_adb_devices()
    except Exception as e:
        logging.debug(f"find_adb_devices failed: {e}")
        return None
    return devices[0] if devices else None


def _adb_shell_ready(device) -> bool:
    """
    A device entry appears in adb long before Android is usable.

    Measured on MuMu 12: the entry shows up at ~3 s (offline), MuMuManager
    reports is_android_started at ~7 s, and `adb shell` only answers at ~13 s.
    Probing with a real shell command is the only reliable readiness signal.
    """
    try:
        output = subprocess.check_output(
            [str(device.adb_path.absolute()), "-s", device.address, "shell", "echo", "ok"],
            encoding="utf-8",
            errors="replace",
            timeout=10,
            stderr=subprocess.DEVNULL,
        )
    except Exception as e:
        logging.debug(f"adb shell probe failed: {e}")
        return False
    return "ok" in output


def _mumu_instance_state(manager, instance_index):
    """
    Return the MuMu instance state, or None when it cannot be read.

    Keys: process (the VM runs), android (Android finished booting),
    host / port (its adb endpoint).
    """
    if manager is None:
        return None
    try:
        output = subprocess.check_output(
            [str(manager), "info", "-v", str(instance_index)],
            encoding="utf-8",
            errors="replace",
            timeout=15,
            stderr=subprocess.DEVNULL,
        )
        data = json.loads(output)
    except Exception as e:
        logging.debug(f"MuMuManager info failed: {e}")
        return None

    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict):
        return None
    return {
        "process": bool(data.get("is_process_started")),
        "android": bool(data.get("is_android_started")),
        "host": data.get("adb_host_ip") or "127.0.0.1",
        "port": data.get("adb_port"),
    }


def _launch_mumu(manager, instance_index) -> bool:
    logging.info(f"Launching MuMu Player instance {instance_index}: {manager}")
    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [str(manager), "control", "-v", str(instance_index), "launch"],
            creationflags=creation_flags,
        )
        return True
    except OSError as e:
        logging.error(f"Failed to launch MuMu Player: {e}")
        return False


def ensure_emulator_running(timeout: float = 180.0):
    """
    Start the MuMu Player instance when it is not usable yet, and wait until
    Android is actually booted (not merely listed by adb).

    The wait is cancellable: pressing stop sets stop_event and aborts startup
    within a couple of seconds instead of burning through the whole timeout.
    A launch that silently does nothing is retried once a minute, and the
    current VM state is reported every 10 s so a stuck boot is visible.

    Only MuMu can be started this way. For LDPlayer the emulator has to be
    launched by the user, so a missing device is then reported by init_maa().
    """
    instance_index = str((config or {}).get("emulator", {}).get("mumu_index", 0))

    device = _first_adb_device()
    if device is not None and _adb_shell_ready(device):
        logging.info("Emulator already running and reachable over ADB.")
        return

    install_dir = _find_mumu_install_dir()
    manager = (install_dir / MUMU_MANAGER_RELATIVE_PATH) if install_dir else None
    adb_path = (install_dir / "nx_main" / "adb.exe") if install_dir else None

    state = _mumu_instance_state(manager, instance_index)
    vm_running = bool(state and state["process"])

    if vm_running:
        # Android may well be up already; adb just has not been told about it.
        logging.info("Emulator is already running; making sure adb can reach it.")
    elif manager is None:
        logging.warning("MuMu Player installation not found; assuming the emulator is started manually.")
        return
    elif not _launch_mumu(manager, instance_index):
        return

    started = time.time()
    deadline = started + timeout
    last_report = 0.0
    last_connect = 0.0
    next_relaunch = started + 60

    while time.time() < deadline:
        if stop_event.is_set():
            logging.warning("Stop requested while waiting for the emulator; aborting startup.")
            raise RuntimeError("Cancelled while waiting for the emulator to boot.")

        device = _first_adb_device()
        if device is not None and _adb_shell_ready(device):
            logging.info(
                f"Emulator is up and reachable over ADB ({device.address}) "
                f"after {time.time() - started:.0f}s."
            )
            return

        now = time.time()
        state = _mumu_instance_state(manager, instance_index)
        vm_running = bool(state and state["process"])

        # adb does not always discover the emulator on its own, especially once
        # the adb server has been restarted. Connecting explicitly is what makes
        # a booted-but-invisible instance usable.
        if adb_path is not None and adb_path.exists() and state and state.get("port"):
            if now - last_connect >= 5:
                last_connect = now
                endpoint = f"{state['host']}:{state['port']}"
                try:
                    subprocess.run(
                        [str(adb_path), "connect", endpoint],
                        encoding="utf-8", errors="replace", timeout=15,
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    )
                    logging.debug(f"adb connect {endpoint} attempted")
                except Exception as e:
                    logging.debug(f"adb connect {endpoint} failed: {e}")

        if now - last_report >= 10:
            last_report = now
            detail = f"{device.address} (adb offline)" if device else "no adb device"
            if state is not None:
                detail += f", vm running={state['process']}, android booted={state['android']}"
            logging.info(
                f"Waiting for the emulator to finish booting... [{detail}, {now - started:.0f}s]"
            )

        # MuMu occasionally swallows the launch silently; retry while the VM is down.
        if manager is not None and now >= next_relaunch:
            next_relaunch = now + 60
            if state is not None and not vm_running:
                logging.warning("MuMu instance is not running; launching it again.")
                _launch_mumu(manager, instance_index)

        time.sleep(2)

    raise RuntimeError(f"Emulator did not become usable within {timeout:.0f}s.")


def init():
    global IS_INITIALISED
    if IS_INITIALISED:
        logging.info("Components are already initialised. Skipping.")
        return
    try:
        logging.info(f"autodoriUI {version.VERSION} initialising.")
        if LIVEMODE == "boot_auto":
            ensure_emulator_running()
        init_maa()
        init_player_and_mnt()
        IS_INITIALISED = True
    except Exception as e:
        IS_INITIALISED = False
        logging.error("Initialisation failed.", exc_info=True)
        raise e


def _stop_stale_mnt():
    global mnt
    if mnt is not None:
        try:
            mnt.stop()
        except Exception as e:
            logging.debug(f"Stopping the stale minitouch session failed: {e}")
        mnt = None


def _wait_android_boot_completed(timeout: float = 90.0):
    """
    Wait until Android reports sys.boot_completed=1.

    adb starts answering `shell echo` very early in the guest boot, but the
    MuMu rendering service (nemu IPC) comes up much later - capturing right
    after the adb probe fails with rpc error 1722 for a while. Only needed
    after a VM (re)launch; the property read is a no-op on a settled system.
    """
    device = _first_adb_device()
    if device is None:
        return
    started = time.time()
    deadline = started + timeout
    while time.time() < deadline:
        if stop_event.is_set():
            raise RuntimeError("Cancelled while waiting for Android to finish booting.")
        try:
            output = subprocess.check_output(
                [str(device.adb_path.absolute()), "-s", device.address,
                 "shell", "getprop", "sys.boot_completed"],
                encoding="utf-8", errors="replace", timeout=10,
                stderr=subprocess.DEVNULL,
            )
        except Exception:
            output = ""
        if output.strip() == "1":
            logging.info(f"Android boot completed after {time.time() - started:.0f}s.")
            return
        time.sleep(2)
    logging.warning(f"Android did not report boot completion within {timeout:.0f}s; trying anyway.")


def _game_process_running() -> bool:
    device = _first_adb_device()
    if device is None:
        return False
    try:
        output = subprocess.check_output(
            [str(device.adb_path.absolute()), "-s", device.address, "shell", "pidof", GAME_PACKAGE],
            encoding="utf-8", errors="replace", timeout=10, stderr=subprocess.DEVNULL,
        )
    except Exception:
        return False
    return bool(output.strip())


def _ensure_game_running(timeout: float = 90.0) -> bool:
    """
    Start the game before the task, if it is not running.

    MAA resolves its MuMu capture display when it connects. Connecting while the
    game is still absent - the usual case right after a cold emulator start -
    leaves every later screencap failing with "Failed to capture display
    [ret=2] / Image is empty", and the task then spins without ever seeing a
    frame. Having the game up first avoids that state.
    """
    if _game_process_running():
        return True
    device = _first_adb_device()
    if device is None:
        return False
    logging.info("Game is not running; launching it before the task starts.")
    try:
        subprocess.run(
            [str(device.adb_path.absolute()), "-s", device.address, "shell", "monkey",
             "-p", GAME_PACKAGE, "-c", "android.intent.category.LAUNCHER", "1"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=30,
        )
    except Exception as e:
        logging.warning(f"Launching the game failed: {e}")
        return False
    started = time.time()
    while time.time() - started < timeout:
        if stop_event.is_set():
            raise RuntimeError("Cancelled while waiting for the game to start.")
        if _game_process_running():
            logging.info(f"Game started after {time.time() - started:.0f}s.")
            return True
        time.sleep(2)
    logging.warning("The game did not report a process in time; continuing anyway.")
    return False


def _verify_maa_capture(attempts: int = 3) -> bool:
    """
    Check that MAA itself can take a screenshot, reconnecting if it cannot.

    Reconnecting re-resolves MuMu's display for the game, which is what fixes
    the "capture connected before the game existed" state.
    """
    if maacontroller is None:
        return False
    for attempt in range(1, attempts + 1):
        try:
            image = maacontroller.post_screencap().wait().get()
        except Exception as e:
            image = None
            logging.debug(f"MAA screencap probe raised: {e}")
        if image is not None:
            if attempt > 1:
                logging.info("MAA capture works again after reconnecting the controller.")
            return True
        logging.warning(
            f"MAA cannot capture the screen (attempt {attempt}/{attempts}); reconnecting the controller."
        )
        try:
            maacontroller.post_connection().wait()
        except Exception as e:
            logging.debug(f"Controller reconnect failed: {e}")
        time.sleep(5)
    return False


def revalidate_emulator_stack():
    """
    Make sure the capture/touch stack still matches the running emulator.

    The app is long-lived (tray + scheduled runs) while the emulator VM is
    not: MuMu can be closed, crash, or get restarted during the day. A VM
    restart invalidates both the MuMu IPC capture handle (capture then
    returned None and crashed the task with 'NoneType' is not subscriptable)
    and the minitouch session that ran inside the old VM. Probe the capture
    path before every task: the probe itself rebuilds the IPC handle, and if
    that happened the minitouch session is rebuilt as well. Also catches the
    VM being down at all (observed as a scheduled run flailing on dead
    captures for minutes): boot_auto then brings the emulator back first.
    """
    if current_player is None:
        return
    if LIVEMODE == "boot_auto":
        ensure_emulator_running()
    try:
        current_player.ipc_capture_display()
    except Exception:
        if LIVEMODE != "boot_auto":
            raise
        # A freshly relaunched VM answers adb long before its rendering
        # service is up - and the service can lag behind sys.boot_completed
        # too on a cold start. Wait for Android, then give the capture a few
        # more rounds before declaring the emulator unusable.
        _wait_android_boot_completed()
        last = None
        for _ in range(6):
            try:
                current_player.ipc_capture_display()
                break
            except Exception as e:
                last = e
                time.sleep(5)
        else:
            raise RuntimeError(f"Emulator capture is not usable ({last}); cannot start the task.")

    # A cold VM produces a launcher frame long before its compositor settles, so
    # a single successful capture is not proof that the display is stable. Ask
    # for a few consecutive frames; MAA binds its own handle at connect time and
    # captured a dead display for minutes when it connected 12 s after the VM
    # process started.
    for _ in range(3):
        current_player.ipc_capture_display()
        time.sleep(1)

    if getattr(current_player, "reconnected", False):
        current_player.reconnected = False
        logging.warning(
            "Emulator was restarted since the last task; rebuilding the capture/touch stack."
        )
        _stop_stale_mnt()
        init_player_and_mnt()
        # The controller's own MuMu capture handle died with the old VM; ask
        # it to reconnect. If that fails it keeps working through its ADB
        # screencap fallback, just slower.
        try:
            maacontroller.post_connection().wait()
        except Exception as e:
            logging.debug(f"Controller reconnect after VM restart failed: {e}")

    # MAA binds its own handle, so the probe above says nothing about whether
    # the pipeline can see the screen. Check it for real: starting a task on a
    # controller that cannot capture only spins on "Image is empty".
    if LIVEMODE == "boot_auto":
        _ensure_game_running()
    if not _verify_maa_capture():
        raise RuntimeError(
            "The controller cannot capture the emulator screen; restarting the session later."
        )


def is_connection_error(exc) -> bool:
    """True when the failure means the device pipe died, not a timing bug."""
    if isinstance(exc, (BrokenPipeError, ConnectionError)):
        return True
    return isinstance(exc, OSError) and getattr(exc, "errno", None) in CONNECTION_ERRNOS


def rebuild_touch_session() -> bool:
    """
    Rebuild the minitouch session after its adb pipe died mid-song.

    The session lives inside the emulator VM, so a VM crash or restart kills the
    adb shell that carries it and every later write raises OSError. Bring the
    emulator back, then create a fresh session.
    """
    logging.warning("Rebuilding the minitouch session after the connection loss.")
    _stop_stale_mnt()
    try:
        ensure_emulator_running()
        device = _first_adb_device()
        if device is None or not _adb_shell_ready(device):
            logging.error("The emulator is still unreachable; the touch session stays down.")
            return False
        init_player_and_mnt()
        logging.info("Minitouch session rebuilt.")
        return True
    except Exception as e:
        logging.error(f"Rebuilding the minitouch session failed: {e}")
        return False


def shutdown_resources():
    global mnt, maacontroller, maatasker

    if maatasker and maatasker.running:
        logging.info("Final shutdown: Stopping MAA tasker.")
        maatasker.post_stop()

    if mnt:
        logging.info("Disconnecting Minitouch...")
        try:
            mnt.stop()
            mnt = None
        except Exception as e:
            logging.error(f"Error disconnecting Minitouch: {e}", exc_info=True)

    if maacontroller:
        logging.info("Releasing MAA AdbController...")
        maacontroller = None


def run_task_mode():
    global DIFFICULTY, HUMAN_DELAY_ENABLED, LIVEMODE
    global MAX_CONTINUOUS_NOT_FC_COUNT, MAX_ATTEMPT_COUNT, PLAY_FAILED_TIMES, NOT_FC_SONG_COUNT_DICT
    global PLAY_COMPLETED_COUNT, RECOVER_COUNT, SONG_ATTEMPT_COUNT_DICT

    if not maacontroller or not mnt:
        raise RuntimeError("MAA is not initialised.")

    revalidate_emulator_stack()

    PLAY_FAILED_TIMES = 0
    PLAY_COMPLETED_COUNT = 0
    RECOVER_COUNT = 0
    NOT_FC_SONG_COUNT_DICT.clear()
    SONG_ATTEMPT_COUNT_DICT.clear()  # MAX_ATTEMPT_COUNT is documented as per task session

    mode_configs = {
        "free_single": ("Single", "ui_simplified_entry", ["single.json"]),
        "medley_single": ("Medley", "ui_simplified_entry", ["single.json"]),
        "story": ("story", "read_story", ["story.json"]),
        "rouge": ("rouge", "start_from_main_menu", ["rouge.json"]),
        "free_auto": ("Auto", "select_song", ["full_auto.json"]),
        "boot_auto": ("Boot Auto", "boot_launch", ["full_auto.json", "boot_auto.json"]),
    }

    if LIVEMODE not in mode_configs:
        logging.error(f"Unknown mode: {LIVEMODE}")
        return

    display_name, entry_task, json_files = mode_configs[LIVEMODE]

    pipeline_def_path = data_path("assets/resource/pipeline")
    override_pipeline = {}

    for json_filename in json_files:
        file_path = pipeline_def_path / json_filename
        if file_path.exists():
            with open(file_path, 'r', encoding='utf-8') as f:
                try:
                    data = json.load(f)
                    override_pipeline.update(data)
                except json.JSONDecodeError as e:
                    logging.error(f"Parsing {json_filename} failed, please check JSON format: {e}")
                    return
        else:
            logging.error(f"Cannot find required Pipeline configuration file: {file_path}")
            return

    logging.info(f"Submitting {display_name} Mode auto-play task. Entry: {entry_task}")
    maatasker.post_task(entry_task, override_pipeline).wait()
    logging.info(f"{display_name} Mode task finished or stopped.")
