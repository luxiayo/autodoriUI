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
from chart import Chart, PlayRecord
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
PHOTOGATE_LATENCY = 30
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
PLAY_FAILED_TIMES = 0
HUMAN_DELAY_ENABLED = False
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
                    matched_song_name = "[超高難易度 SPECIAL] " + matched_song_name

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
            global PLAY_COMPLETED_COUNT

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
                context.run_action("close_app")
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
            return CustomAction.RunResult(False)
        finally:
            monitor.join(timeout=5)


@maaresource.custom_action("SaveSong")
class SaveSong(CustomAction):
    def run(self, context: Context, argv: CustomAction.RunArg):
        name: CustomRecognitionResult = argv.reco_detail.best_result.detail
        save_song(name)
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
            output = subprocess.check_output(command_list, text=True, timeout=10)
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

    is_connected = False
    for i in range(3):
        if maacontroller.post_connection().wait().succeeded:
            is_connected = True
            break

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


def _adb_devices_available():
    try:
        return bool(Toolkit.find_adb_devices())
    except Exception as e:
        logging.debug(f"find_adb_devices failed: {e}")
        return False


def ensure_emulator_running(timeout: float = 180.0):
    """
    Start the MuMu Player instance when no ADB device is reachable yet.

    Only MuMu can be started this way. For LDPlayer the emulator has to be
    launched by the user, so a missing device is then reported by init_maa().
    """
    if _adb_devices_available():
        logging.info("Emulator already running, skipping launch.")
        return

    install_dir = _find_mumu_install_dir()
    if install_dir is None:
        logging.warning("MuMu Player installation not found; assuming the emulator is started manually.")
        return

    manager = install_dir / MUMU_MANAGER_RELATIVE_PATH
    instance_index = str((config or {}).get("emulator", {}).get("mumu_index", 0))
    logging.info(f"Launching MuMu Player instance {instance_index}: {manager}")

    creation_flags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        subprocess.Popen(
            [str(manager), "control", "-v", instance_index, "launch"],
            creationflags=creation_flags,
        )
    except OSError as e:
        logging.error(f"Failed to launch MuMu Player: {e}")
        return

    deadline = time.time() + timeout
    while time.time() < deadline:
        time.sleep(5)
        if _adb_devices_available():
            logging.info("Emulator is up and reachable over ADB.")
            return
        logging.info("Waiting for the emulator to become reachable over ADB...")

    raise RuntimeError(f"Emulator did not come up within {timeout:.0f}s.")


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


def shutdown_resources():
    global mnt, maacontroller, maatasker

    if maatasker and maatasker.running:
        logging.info("Final shutdown: Stopping MAA tasker.")
        maatasker.post_stop()

    if mnt:
        logging.info("Disconnecting Minitouch...")
        try:
            mnt.disconnect()
            mnt = None
        except Exception as e:
            logging.error(f"Error disconnecting Minitouch: {e}", exc_info=True)

    if maacontroller:
        logging.info("Disconnecting MAA AdbController...")
        try:
            if maacontroller.post_disconnect().wait().succeeded:
                logging.info("AdbController disconnected successfully.")
            else:
                logging.warning("Failed to disconnect AdbController cleanly.")
            maacontroller = None
        except Exception as e:
            logging.error(f"Error disconnecting AdbController: {e}", exc_info=True)


def run_task_mode():
    global DIFFICULTY, HUMAN_DELAY_ENABLED, LIVEMODE
    global MAX_CONTINUOUS_NOT_FC_COUNT, MAX_ATTEMPT_COUNT, PLAY_FAILED_TIMES, NOT_FC_SONG_COUNT_DICT
    global PLAY_COMPLETED_COUNT, SONG_ATTEMPT_COUNT_DICT

    if not maacontroller or not mnt:
        raise RuntimeError("MAA is not initialised.")

    PLAY_FAILED_TIMES = 0
    PLAY_COMPLETED_COUNT = 0
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