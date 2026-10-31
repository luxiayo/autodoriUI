import logging
import time

import mumuipc
import ldipc


class Player:
    def __init__(self, type_: str, path: str, index: int) -> None:
        self.type = type_
        self.display_id = -1
        # Set when a capture failure forced a reconnect: the emulator VM was
        # restarted underneath us, so anything that lives inside the old VM
        # (the minitouch session) needs rebuilding too. Reset by whoever acts
        # on it, e.g. run_task_mode rebuilding the touch stack.
        self.reconnected = False
        if type_ == "mumuv4":
            self.player = mumuipc.MuMuPlayer(path, index, "v4")
        elif type_ == "mumuv5":
            self.player = mumuipc.MuMuPlayer(path, index, "v5")
        elif type_ == "ld":
            self.player = ldipc.LDPlayer(path, index)

    @property
    def resolution(self):
        return self.player.resolution

    def reconnect(self):
        """Build a fresh MuMu IPC connection after the VM was restarted."""
        self.display_id = -1
        self.player.resolution = None
        # Deliberately NOT calling nemu_disconnect on the dead handle: it
        # cannot reach the gone endpoint and leaves the DLL's RPC state
        # poisoned - every later capture fails with rpc error 1722 even with
        # a brand-new handle (measured). Connecting again without the
        # disconnect works immediately; the dead slot just leaks a few bytes
        # on a VM that no longer exists.
        if not self.player.ipc_connect():
            raise RuntimeError("MuMu IPC reconnect failed; is the emulator running?")

    def ipc_capture_display(self):
        if self.type.startswith("mumu"):
            if self.display_id == -1:
                # nemu_get_display_id answers -1 (or 0) when the game is not
                # running yet; display 0 is the VM's main screen either way.
                self.display_id = max(
                    0, self.player.ipc_get_display_id("com.bilibili.star.bili")
                )
            # RGBA -> RGB
            frame = self.player.ipc_capture_display(self.display_id)
            if frame is None:
                # nemu_capture_display answers failure when the VM process is
                # gone or was restarted: the handle connected to the old VM is
                # dead (observed when MuMu went down and came back while the
                # app sat in the tray). Reconnect and retry for a while - adb
                # answers long before the VM's rendering service is ready
                # (rpc error 1722 right after boot), so a single immediate
                # retry is not enough.
                logging.warning(
                    "MuMu IPC capture failed; the emulator was probably "
                    "restarted. Reconnecting."
                )
                self.reconnect()
                self.reconnected = True
                # reconnect() resets display_id to -1; resolve it again or the
                # retry loop below would capture display -1, which fails no
                # matter how healthy the emulator is.
                if self.display_id == -1:
                    self.display_id = max(
                        0, self.player.ipc_get_display_id("com.bilibili.star.bili")
                    )
                deadline = time.time() + 30
                while frame is None and time.time() < deadline:
                    time.sleep(1)
                    # The first call after the reset only refreshes the
                    # resolution and returns None; later calls deliver frames
                    # once the renderer is up.
                    frame = self.player.ipc_capture_display(self.display_id)
            if frame is None:
                raise RuntimeError(
                    "MuMu IPC capture failed even after reconnecting."
                )
            return frame[:, :, :3]
        else:
            # RGB
            return self.player.capture()
